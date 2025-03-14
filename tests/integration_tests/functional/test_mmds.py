# Copyright 2018 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests that verify MMDS related functionality."""

# pylint: disable=too-many-lines
import json
import random
import string
import time
import pytest
from framework.artifacts import DEFAULT_DEV_NAME, NetIfaceConfig,\
    ArtifactCollection
from framework.builder import MicrovmBuilder, SnapshotBuilder, SnapshotType
from framework.utils import generate_mmds_session_token, configure_mmds, \
    generate_mmds_get_request, get_firecracker_version_from_toml, \
    compare_versions
from conftest import _test_images_s3_bucket

import host_tools.network as net_tools

# Minimum lifetime of token.
MIN_TOKEN_TTL_SECONDS = 1
# Maximum lifetime of token.
MAX_TOKEN_TTL_SECONDS = 21600
# Default IPv4 value for MMDS.
DEFAULT_IPV4 = '169.254.169.254'
# MMDS versions supported.
MMDS_VERSIONS = ['V2', 'V1']


def _run_guest_cmd(ssh_connection, cmd, expected, use_json=False):
    _, stdout, stderr = ssh_connection.execute_command(cmd)
    assert stderr.read() == ''
    stdout = stdout.read() if not use_json else json.loads(stdout.read())
    assert stdout == expected


def _populate_data_store(test_microvm, data_store):
    response = test_microvm.mmds.get()
    assert test_microvm.api_session.is_status_ok(response.status_code)
    assert response.json() == {}

    response = test_microvm.mmds.put(json=data_store)
    assert test_microvm.api_session.is_status_no_content(response.status_code)

    response = test_microvm.mmds.get()
    assert test_microvm.api_session.is_status_ok(response.status_code)
    assert response.json() == data_store


def _validate_mmds_snapshot(
    vm_instance,
    vm_builder,
    version,
    target_fc_version=None,
    fc_path=None,
    jailer_path=None
):
    """Test MMDS behaviour across snap-restore."""
    basevm = vm_instance.vm
    root_disk = vm_instance.disks[0]
    disks = [root_disk.local_path()]
    ssh_key = vm_instance.ssh_key
    ipv4_address = '169.254.169.250'

    # Configure MMDS version with custom IPv4 address.
    configure_mmds(
        basevm,
        version=version,
        iface_ids=[DEFAULT_DEV_NAME],
        ipv4_address=ipv4_address,
        fc_version=target_fc_version
    )

    # Check if the FC version supports the latest format for mmds-config.
    # If target_fc_version is None, we assume the current version is used.
    if target_fc_version is None or \
            (target_fc_version is not None and
             compare_versions(target_fc_version, "1.0.0") >= 0):
        expected_mmds_config = {
            "version": version,
            "ipv4_address": ipv4_address,
            "network_interfaces": [DEFAULT_DEV_NAME]
        }
        response = basevm.full_cfg.get()
        assert basevm.api_session.is_status_ok(response.status_code)
        assert response.json()["mmds-config"] == expected_mmds_config

    data_store = {
        'latest': {
            'meta-data': {
                'ami-id': 'ami-12345678'
            }
        }
    }
    _populate_data_store(basevm, data_store)

    basevm.start()

    snapshot_builder = SnapshotBuilder(basevm)

    ssh_connection = net_tools.SSHConnection(basevm.ssh_config)
    _run_guest_cmd(ssh_connection, f'ip route add {ipv4_address} dev eth0', '')

    # Generate token if needed.
    token = None
    if version == "V2":
        token = generate_mmds_session_token(
            ssh_connection,
            ipv4_address,
            token_ttl=60
        )

    # Fetch metadata.
    cmd = generate_mmds_get_request(
        ipv4_address,
        token=token,
    )
    _run_guest_cmd(ssh_connection, cmd, data_store, use_json=True)

    # Create snapshot.
    snapshot = snapshot_builder.create(disks,
                                       ssh_key,
                                       SnapshotType.FULL,
                                       target_version=target_fc_version)

    # Resume microVM and ensure session token is still valid on the base.
    response = basevm.vm.patch(state='Resumed')
    assert basevm.api_session.is_status_no_content(response.status_code)

    # Fetch metadata again using the same token.
    _run_guest_cmd(ssh_connection, cmd, data_store, use_json=True)

    # Kill base microVM.
    basevm.kill()

    # Load microVM clone from snapshot.
    microvm, _ = vm_builder.build_from_snapshot(snapshot,
                                                resume=True,
                                                fc_binary=fc_path,
                                                jailer_binary=jailer_path)

    ssh_connection = net_tools.SSHConnection(microvm.ssh_config)

    # Check the reported mmds config. In versions up to (including) v1.0.0 this
    # was not populated after restore.
    if target_fc_version is not None and \
            compare_versions("1.0.0", target_fc_version) < 0:
        response = microvm.full_cfg.get()
        assert microvm.api_session.is_status_ok(response.status_code)
        assert response.json()["mmds-config"] == expected_mmds_config

    if version == 'V1':
        # Verify that V2 requests don't work
        assert generate_mmds_session_token(
            ssh_connection,
            ipv4_address,
            token_ttl=60
        ) == "Not allowed HTTP method."

        token = None
    else:
        # Attempting to reuse the token across a restore must fail.
        cmd = generate_mmds_get_request(ipv4_address, token=token)
        _run_guest_cmd(ssh_connection, cmd, 'MMDS token not valid.')

        # Generate token.
        token = generate_mmds_session_token(
            ssh_connection,
            ipv4_address,
            token_ttl=60
        )

    # Data store is empty after a restore.
    cmd = generate_mmds_get_request(ipv4_address, token=token)
    _run_guest_cmd(ssh_connection, cmd, 'null')

    # Now populate the store.
    _populate_data_store(microvm, data_store)

    # Fetch metadata.
    _run_guest_cmd(ssh_connection, cmd, data_store, use_json=True)


@pytest.mark.parametrize(
    "version",
    MMDS_VERSIONS
)
def test_custom_ipv4(test_microvm_with_api, network_config, version):
    """
    Test the API for MMDS custom ipv4 support.

    @type: functional
    """
    test_microvm = test_microvm_with_api
    test_microvm.spawn()

    data_store = {
        'latest': {
            'meta-data': {
                'ami-id': 'ami-12345678',
                'reservation-id': 'r-fea54097',
                'local-hostname': 'ip-10-251-50-12.ec2.internal',
                'public-hostname': 'ec2-203-0-113-25.compute-1.amazonaws.com',
                'network': {
                    'interfaces': {
                        'macs': {
                            '02:29:96:8f:6a:2d': {
                                'device-number': '13345342',
                                'local-hostname': 'localhost',
                                'subnet-id': 'subnet-be9b61d'
                            }
                        }
                    }
                }
            }
        }
    }
    _populate_data_store(test_microvm, data_store)

    # Attach network device.
    _tap = test_microvm.ssh_network_config(network_config, '1')

    # Invalid values IPv4 address.
    response = test_microvm.mmds.put_config(json={
        'ipv4_address': '',
        'network_interfaces': ['1']
    })
    assert test_microvm.api_session.is_status_bad_request(response.status_code)

    response = test_microvm.mmds.put_config(json={
        'ipv4_address': '1.1.1.1',
        'network_interfaces': ['1']
    })
    assert test_microvm.api_session.is_status_bad_request(response.status_code)

    ipv4_address = '169.254.169.250'
    # Configure MMDS with custom IPv4 address.
    configure_mmds(
        test_microvm,
        iface_ids=['1'],
        version=version,
        ipv4_address=ipv4_address
    )

    test_microvm.basic_config(vcpu_count=1)
    test_microvm.start()
    ssh_connection = net_tools.SSHConnection(test_microvm.ssh_config)

    _run_guest_cmd(ssh_connection, f'ip route add {ipv4_address} dev eth0', '')

    token = None
    if version == 'V2':
        # Generate token.
        token = generate_mmds_session_token(
            ssh_connection,
            ipv4_address,
            token_ttl=60
        )

    pre = generate_mmds_get_request(
        ipv4_address,
        token=token,
    )

    cmd = pre + 'latest/meta-data/ami-id'
    _run_guest_cmd(ssh_connection, cmd, 'ami-12345678', use_json=True)

    # The request is still valid if we append a
    # trailing slash to a leaf node.
    cmd = pre + 'latest/meta-data/ami-id/'
    _run_guest_cmd(ssh_connection, cmd, 'ami-12345678', use_json=True)

    cmd = pre + 'latest/meta-data/network/interfaces/macs/' \
                '02:29:96:8f:6a:2d/subnet-id'
    _run_guest_cmd(ssh_connection, cmd, 'subnet-be9b61d', use_json=True)

    # Test reading a non-leaf node WITHOUT a trailing slash.
    cmd = pre + 'latest/meta-data'
    _run_guest_cmd(ssh_connection, cmd,
                   data_store['latest']['meta-data'], use_json=True)

    # Test reading a non-leaf node with a trailing slash.
    cmd = pre + 'latest/meta-data/'
    _run_guest_cmd(ssh_connection, cmd,
                   data_store['latest']['meta-data'], use_json=True)


@pytest.mark.parametrize(
    "version",
    MMDS_VERSIONS
)
def test_json_response(test_microvm_with_api, network_config, version):
    """
    Test the MMDS json response.

    @type: functional
    """
    test_microvm = test_microvm_with_api
    test_microvm.spawn()

    data_store = {
        'latest': {
            'meta-data': {
                'ami-id': 'ami-12345678',
                'reservation-id': 'r-fea54097',
                'local-hostname': 'ip-10-251-50-12.ec2.internal',
                'public-hostname': 'ec2-203-0-113-25.compute-1.amazonaws.com',
                'dummy_res': ['res1', 'res2']
            },
            "Limits": {
                "CPU": 512,
                "Memory": 512
            },
            "Usage": {
                "CPU": 12.12
            }
        }
    }

    # Attach network device.
    _tap = test_microvm.ssh_network_config(network_config, '1')

    # Configure MMDS version.
    configure_mmds(test_microvm, iface_ids=['1'], version=version)

    # Populate data store with contents.
    _populate_data_store(test_microvm, data_store)

    test_microvm.basic_config(vcpu_count=1)
    test_microvm.start()
    ssh_connection = net_tools.SSHConnection(test_microvm.ssh_config)

    cmd = 'ip route add {} dev eth0'.format(DEFAULT_IPV4)
    _run_guest_cmd(ssh_connection, cmd, '')

    token = None
    if version == 'V2':
        # Generate token.
        token = generate_mmds_session_token(
            ssh_connection,
            DEFAULT_IPV4,
            token_ttl=60
        )

    pre = generate_mmds_get_request(DEFAULT_IPV4, token)

    cmd = pre + 'latest/meta-data/'
    _run_guest_cmd(ssh_connection, cmd,
                   data_store['latest']['meta-data'], use_json=True)

    cmd = pre + 'latest/meta-data/ami-id/'
    _run_guest_cmd(ssh_connection, cmd, 'ami-12345678', use_json=True)

    cmd = pre + 'latest/meta-data/dummy_res/0'
    _run_guest_cmd(ssh_connection, cmd, 'res1', use_json=True)

    cmd = pre + 'latest/Usage/CPU'
    _run_guest_cmd(ssh_connection, cmd, 12.12, use_json=True)

    cmd = pre + 'latest/Limits/CPU'
    _run_guest_cmd(ssh_connection, cmd, 512, use_json=True)


@pytest.mark.parametrize(
    "version",
    MMDS_VERSIONS
)
def test_mmds_response(test_microvm_with_api, network_config, version):
    """
    Test MMDS responses to various datastore requests.

    @type: functional
    """
    test_microvm = test_microvm_with_api
    test_microvm.spawn()

    data_store = {
        'latest': {
            'meta-data': {
                'ami-id': 'ami-12345678',
                'reservation-id': 'r-fea54097',
                'local-hostname': 'ip-10-251-50-12.ec2.internal',
                'public-hostname': 'ec2-203-0-113-25.compute-1.amazonaws.com',
                'dummy_obj': {
                    'res_key': 'res_value',
                },
                'dummy_array': [
                    'arr_val1',
                    'arr_val2'
                ]
            },
            "Limits": {
                "CPU": 512,
                "Memory": 512
            },
            "Usage": {
                "CPU": 12.12
            }
        }
    }

    # Attach network device.
    _tap = test_microvm.ssh_network_config(network_config, '1')

    # Configure MMDS version.
    configure_mmds(test_microvm, iface_ids=['1'], version=version)
    # Populate data store with contents.
    _populate_data_store(test_microvm, data_store)

    test_microvm.basic_config(vcpu_count=1)
    test_microvm.start()
    ssh_connection = net_tools.SSHConnection(test_microvm.ssh_config)

    cmd = 'ip route add {} dev eth0'.format(DEFAULT_IPV4)
    _run_guest_cmd(ssh_connection, cmd, '')

    token = None
    if version == 'V2':
        # Generate token.
        token = generate_mmds_session_token(
            ssh_connection,
            DEFAULT_IPV4,
            token_ttl=60
        )

    pre = generate_mmds_get_request(
        DEFAULT_IPV4,
        token=token,
        app_json=False
    )

    cmd = pre + 'latest/meta-data/'
    expected = "ami-id\n" \
               "dummy_array\n" \
               "dummy_obj/\n" \
               "local-hostname\n" \
               "public-hostname\n" \
               "reservation-id"

    _run_guest_cmd(ssh_connection, cmd, expected)

    cmd = pre + 'latest/meta-data/ami-id/'
    _run_guest_cmd(ssh_connection, cmd, 'ami-12345678')

    cmd = pre + 'latest/meta-data/dummy_array/0'
    _run_guest_cmd(ssh_connection, cmd, 'arr_val1')

    cmd = pre + 'latest/Usage/CPU'
    _run_guest_cmd(ssh_connection, cmd, 'Cannot retrieve value. The value has'
                   ' an unsupported type.')

    cmd = pre + 'latest/Limits/CPU'
    _run_guest_cmd(ssh_connection, cmd, 'Cannot retrieve value. The value has'
                   ' an unsupported type.')


@pytest.mark.parametrize(
    "version",
    MMDS_VERSIONS
)
def test_larger_than_mss_payloads(
        test_microvm_with_api,
        network_config,
        version):
    """
    Test MMDS content for payloads larger than MSS.

    @type: functional
    """
    test_microvm = test_microvm_with_api
    test_microvm.spawn()

    # Attach network device.
    _tap = test_microvm.ssh_network_config(network_config, '1')
    # Configure MMDS version.
    configure_mmds(test_microvm, iface_ids=['1'], version=version)

    # The MMDS is empty at this point.
    response = test_microvm.mmds.get()
    assert test_microvm.api_session.is_status_ok(response.status_code)
    assert response.json() == {}

    test_microvm.basic_config(vcpu_count=1)
    test_microvm.start()

    # Make sure MTU is 1500 bytes.
    ssh_connection = net_tools.SSHConnection(test_microvm.ssh_config)

    _run_guest_cmd(ssh_connection, 'ip link set dev eth0 mtu 1500', '')

    cmd = 'ip a s eth0 | grep -i mtu | tr -s " " | cut -d " " -f 4,5'
    _run_guest_cmd(ssh_connection, cmd, 'mtu 1500\n')

    # These values are usually used by booted up guest network interfaces.
    mtu = 1500
    ipv4_packet_headers_len = 20
    tcp_segment_headers_len = 20
    mss = mtu - ipv4_packet_headers_len - tcp_segment_headers_len

    # Generate a random MMDS content, double of MSS.
    letters = string.ascii_lowercase
    larger_than_mss = ''.join(random.choice(letters) for i in range(2 * mss))
    mss_equal = ''.join(random.choice(letters) for i in range(mss))
    lower_than_mss = ''.join(random.choice(letters) for i in range(mss - 2))
    data_store = {
        'larger_than_mss': larger_than_mss,
        'mss_equal': mss_equal,
        'lower_than_mss': lower_than_mss
    }
    response = test_microvm.mmds.put(json=data_store)
    assert test_microvm.api_session.is_status_no_content(response.status_code)

    response = test_microvm.mmds.get()
    assert test_microvm.api_session.is_status_ok(response.status_code)
    assert response.json() == data_store

    _run_guest_cmd(ssh_connection, f'ip route add {DEFAULT_IPV4} dev eth0', '')

    token = None
    if version == 'V2':
        # Generate token.
        token = generate_mmds_session_token(
            ssh_connection,
            DEFAULT_IPV4,
            token_ttl=60
        )

    pre = generate_mmds_get_request(
        DEFAULT_IPV4,
        token=token,
        app_json=False
    )

    cmd = pre + 'larger_than_mss'
    _run_guest_cmd(ssh_connection, cmd, larger_than_mss)

    cmd = pre + 'mss_equal'
    _run_guest_cmd(ssh_connection, cmd, mss_equal)

    cmd = pre + 'lower_than_mss'
    _run_guest_cmd(ssh_connection, cmd, lower_than_mss)


@pytest.mark.parametrize(
    "version",
    MMDS_VERSIONS
)
def test_mmds_dummy(test_microvm_with_api, network_config, version):
    """
    Test the API and guest facing features of the microVM MetaData Service.

    @type: functional
    """
    test_microvm = test_microvm_with_api
    test_microvm.spawn()

    # Attach network device.
    _tap = test_microvm.ssh_network_config(network_config, '1')
    # Configure MMDS version.
    configure_mmds(test_microvm, iface_ids=['1'], version=version)

    # The MMDS is empty at this point.
    response = test_microvm.mmds.get()
    assert test_microvm.api_session.is_status_ok(response.status_code)
    assert response.json() == {}

    # Test that patch return NotInitialized when the MMDS is not initialized.
    dummy_json = {
        'latest': {
            'meta-data': {
                'ami-id': 'dummy'
            }
        }
    }
    response = test_microvm.mmds.patch(json=dummy_json)
    assert test_microvm.api_session.is_status_bad_request(response.status_code)
    fault_json = {
        "fault_message": "The MMDS data store is not initialized."
    }
    assert response.json() == fault_json

    # Test that using the same json with a PUT request, the MMDS data-store is
    # created.
    response = test_microvm.mmds.put(json=dummy_json)
    assert test_microvm.api_session.is_status_no_content(response.status_code)

    response = test_microvm.mmds.get()
    assert test_microvm.api_session.is_status_ok(response.status_code)
    assert response.json() == dummy_json

    response = test_microvm.mmds.get()
    assert test_microvm.api_session.is_status_ok(response.status_code)
    assert response.json() == dummy_json

    dummy_json = {
        'latest': {
            'meta-data': {
                'ami-id': 'another_dummy',
                'secret_key': 'eaasda48141411aeaeae'
            }
        }
    }
    response = test_microvm.mmds.patch(json=dummy_json)
    assert test_microvm.api_session.is_status_no_content(response.status_code)
    response = test_microvm.mmds.get()
    assert test_microvm.api_session.is_status_ok(response.status_code)
    assert response.json() == dummy_json


@pytest.mark.parametrize(
    "version",
    MMDS_VERSIONS
)
def test_guest_mmds_hang(test_microvm_with_api, network_config, version):
    """
    Test the MMDS json endpoint when Content-Length larger than actual length.

    @type: functional
    """
    test_microvm = test_microvm_with_api
    test_microvm.spawn()

    # Attach network device.
    _tap = test_microvm.ssh_network_config(network_config, '1')
    # Configure MMDS version.
    configure_mmds(test_microvm, iface_ids=['1'], version=version)

    data_store = {
        'latest': {
            'meta-data': {
                'ami-id': 'ami-12345678'
            }
        }
    }
    _populate_data_store(test_microvm, data_store)

    test_microvm.basic_config(vcpu_count=1)
    test_microvm.start()
    ssh_connection = net_tools.SSHConnection(test_microvm.ssh_config)

    _run_guest_cmd(ssh_connection, f'ip route add {DEFAULT_IPV4} dev eth0', '')

    get_cmd = 'curl -m 2 -s'
    get_cmd += ' -X GET'
    get_cmd += ' -H  "Content-Length: 100"'
    get_cmd += ' -H "Accept: application/json"'
    get_cmd += ' -d "some body"'
    get_cmd += f' http://{DEFAULT_IPV4}/'

    if version == 'V1':
        _, stdout, _ = ssh_connection.execute_command(get_cmd)
        assert 'Invalid request' in stdout.read()
    else:
        # Generate token.
        token = generate_mmds_session_token(
            ssh_connection,
            DEFAULT_IPV4,
            token_ttl=60
        )

        get_cmd += ' -H  "X-metadata-token: {}"'.format(token)
        _, stdout, _ = ssh_connection.execute_command(get_cmd)
        assert 'Invalid request' in stdout.read()

        # Do the same for a PUT request.
        cmd = 'curl -m 2 -s'
        cmd += ' -X PUT'
        cmd += ' -H  "Content-Length: 100"'
        cmd += ' -H  "X-metadata-token: {}"'.format(token)
        cmd += ' -H "Accept: application/json"'
        cmd += ' -d "some body"'
        cmd += ' http://{}/'.format(DEFAULT_IPV4)

        _, stdout, _ = ssh_connection.execute_command(cmd)
        assert 'Invalid request' in stdout.read()


@pytest.mark.parametrize(
    "version",
    MMDS_VERSIONS
)
def test_mmds_limit_scenario(test_microvm_with_api, network_config, version):
    """
    Test the MMDS json endpoint when data store size reaches the limit.

    @type: negative
    """
    test_microvm = test_microvm_with_api
    # Set a large enough limit for the API so that requests actually reach the
    # MMDS server.
    test_microvm.jailer.extra_args.update(
        {"http-api-max-payload-size": "512000", "mmds-size-limit": "51200"})
    test_microvm.spawn()

    # Attach network device.
    _tap = test_microvm.ssh_network_config(network_config, '1')
    # Configure MMDS version.
    configure_mmds(test_microvm, iface_ids=['1'], version=version)

    dummy_json = {
        'latest': {
            'meta-data': {
                'ami-id': 'dummy'
            }
        }
    }

    # Populate data-store.
    response = test_microvm.mmds.put(json=dummy_json)
    assert test_microvm.api_session.is_status_no_content(response.status_code)

    # Send a request that will exceed the data store.
    aux = "a" * 51200
    large_json = {
        'latest': {
            'meta-data': {
                'ami-id': "smth",
                'secret_key': aux
            }
        }
    }
    response = test_microvm.mmds.put(json=large_json)
    assert test_microvm.api_session.\
        is_status_payload_too_large(response.status_code)

    response = test_microvm.mmds.get()
    assert response.json() == dummy_json

    # Send a request that will fill the data store.
    aux = "a" * 51137
    dummy_json = {
        'latest': {
            'meta-data': {
                'ami-id': "smth",
                'secret_key': aux
            }
        }
    }
    response = test_microvm.mmds.patch(json=dummy_json)
    assert test_microvm.api_session.is_status_no_content(response.status_code)

    # Try to send a new patch thaw will increase the data store size. Since the
    # actual size is equal with the limit this request should fail with
    # PayloadTooLarge.
    aux = "b" * 10
    dummy_json = {
        'latest': {
            'meta-data': {
                'ami-id': "smth",
                'secret_key2': aux
            }
        }
    }
    response = test_microvm.mmds.patch(json=dummy_json)
    assert test_microvm.api_session.\
        is_status_payload_too_large(response.status_code)
    # Check that the patch actually failed and the contents of the data store
    # has not changed.
    response = test_microvm.mmds.get()
    assert str(response.json()).find(aux) == -1

    # Delete something from the mmds so we will be able to send new data.
    dummy_json = {
        'latest': {
            'meta-data': {
                'ami-id': "smth",
                'secret_key': "a"
            }
        }
    }
    response = test_microvm.mmds.patch(json=dummy_json)
    assert test_microvm.api_session.is_status_no_content(response.status_code)

    # Check that the size has shrunk.
    response = test_microvm.mmds.get()
    assert len(str(response.json()).replace(" ", "")) == 59

    # Try to send a new patch, this time the request should succeed.
    aux = "a" * 100
    dummy_json = {
        'latest': {
            'meta-data': {
                'ami-id': "smth",
                'secret_key': aux
            }
        }
    }
    response = test_microvm.mmds.patch(json=dummy_json)
    assert test_microvm.api_session.is_status_no_content(response.status_code)

    # Check that the size grew as expected.
    response = test_microvm.mmds.get()
    assert len(str(response.json()).replace(" ", "")) == 158


@pytest.mark.parametrize(
    "version",
    MMDS_VERSIONS
)
def test_mmds_snapshot(bin_cloner_path,  version):
    """
    Test MMDS behavior by restoring a snapshot on current and past FC versions.

    Ensures that the version is persisted or initialised with the default if
    the firecracker version does not support it.

    @type: functional
    """
    vm_builder = MicrovmBuilder(bin_cloner_path)
    vm_instance = vm_builder.build_vm_nano(
        net_ifaces=[NetIfaceConfig()]
    )

    # Validate current version.
    _validate_mmds_snapshot(
        vm_instance, vm_builder, version)

    # Validate restoring in past versions.
    artifacts = ArtifactCollection(_test_images_s3_bucket())
    # Fetch all firecracker binaries.
    # Create a snapshot with current build and restore with each FC binary
    # artifact.
    firecracker_artifacts = artifacts.firecrackers(
        # v1.0.0 breaks snapshot compatibility with older versions.
        min_version="1.0.0",
        max_version=get_firecracker_version_from_toml())
    for firecracker in firecracker_artifacts:
        vm_instance = vm_builder.build_vm_nano(
            net_ifaces=[NetIfaceConfig()]
        )
        firecracker.download()
        jailer = firecracker.jailer()
        jailer.download()

        target_version = firecracker.base_name()[1:]
        # If the version is smaller or equal to 1.0.0, we expect that
        # MMDS will be initialised with V1 by default.
        if compare_versions(target_version, "1.0.0") <= 0:
            mmds_version = "V1"
        else:
            mmds_version = version

        _validate_mmds_snapshot(
            vm_instance,
            vm_builder,
            mmds_version,
            target_fc_version=target_version,
            fc_path=firecracker.local_path(),
            jailer_path=jailer.local_path()
        )


def test_mmds_older_snapshot(bin_cloner_path):
    """
    Test MMDS behavior restoring older snapshots in the current version.

    Ensures that the MMDS version is persisted or initialised with the default
    if the FC version does not support this feature.

    @type: functional
    """
    vm_builder = MicrovmBuilder(bin_cloner_path)

    # Validate restoring a past snapshot in the current version.
    artifacts = ArtifactCollection(_test_images_s3_bucket())
    # Fetch all firecracker binaries.
    firecracker_artifacts = artifacts.firecrackers(
        max_version=get_firecracker_version_from_toml())
    for firecracker in firecracker_artifacts:
        firecracker.download()
        jailer = firecracker.jailer()
        jailer.download()

        net_iface = NetIfaceConfig()
        vm_instance = vm_builder.build_vm_nano(
            net_ifaces=[net_iface],
            fc_binary=firecracker.local_path(),
            jailer_binary=jailer.local_path()
        )

        fc_version = firecracker.base_name()[1:]
        # If the version is smaller or equal to 1.0.0, we expect that
        # MMDS will be initialised with V1 by default.
        # Otherwise, we may configure V2.
        if compare_versions(fc_version, "1.0.0") <= 0:
            mmds_version = "V1"
        else:
            mmds_version = "V2"

        # Check if we need to configure MMDS the old way, by
        # setting `allow_mmds_requests`.
        # If we do (for v0.25), reissue the network PUT api call.
        if compare_versions(fc_version, "1.0.0") < 0:
            basevm = vm_instance.vm
            guest_mac = net_tools.mac_from_ip(net_iface.guest_ip)
            response = basevm.network.put(
                iface_id=net_iface.dev_name,
                host_dev_name=net_iface.tap_name,
                guest_mac=guest_mac,
                allow_mmds_requests=True
            )
            assert basevm.api_session.is_status_no_content(
                response.status_code)

        _validate_mmds_snapshot(
            vm_instance,
            vm_builder,
            mmds_version,
            target_fc_version=fc_version
        )


def test_mmds_v2_negative(test_microvm_with_api, network_config):
    """
    Test invalid MMDS GET/PUT requests when using V2.

    @type: negative
    """
    test_microvm = test_microvm_with_api
    test_microvm.spawn()

    # Attach network device.
    _tap = test_microvm.ssh_network_config(network_config, '1')
    # Configure MMDS version.
    configure_mmds(test_microvm, version='V2', iface_ids=['1'])

    data_store = {
        'latest': {
            'meta-data': {
                'ami-id': 'ami-12345678',
                'reservation-id': 'r-fea54097',
                'local-hostname': 'ip-10-251-50-12.ec2.internal',
                'public-hostname': 'ec2-203-0-113-25.compute-1.amazonaws.com'
            }
        }
    }
    _populate_data_store(test_microvm, data_store)

    test_microvm.basic_config(vcpu_count=1)
    test_microvm.start()
    ssh_connection = net_tools.SSHConnection(test_microvm.ssh_config)

    _run_guest_cmd(ssh_connection, f'ip route add {DEFAULT_IPV4} dev eth0', '')

    # Check `GET` request fails when token is not provided.
    cmd = generate_mmds_get_request(DEFAULT_IPV4)
    expected = "No MMDS token provided. Use `X-metadata-token` header " \
               "to specify the session token."
    _run_guest_cmd(ssh_connection, cmd, expected)

    # Generic `GET` request.

    # Check `GET` request fails when token is not valid.
    _run_guest_cmd(ssh_connection, generate_mmds_get_request(
        DEFAULT_IPV4, token="foo"), "MMDS token not valid.")

    # Check `PUT` request fails when token TTL is not provided.
    cmd = f'curl -m 2 -s -X PUT http://{DEFAULT_IPV4}/latest/api/token'
    expected = "Token time to live value not found. Use " \
               "`X-metadata-token-ttl_seconds` header to specify " \
               "the token's lifetime."
    _run_guest_cmd(ssh_connection, cmd, expected)

    # Check `PUT` request fails when `X-Forwarded-For` header is provided.
    cmd = 'curl -m 2 -s'
    cmd += ' -X PUT'
    cmd += ' -H  "X-Forwarded-For: foo"'
    cmd += f' http://{DEFAULT_IPV4}'
    expected = "Invalid header. Reason: Unsupported header name. " \
               "Key: X-Forwarded-For"
    _run_guest_cmd(ssh_connection, cmd, expected)

    # Generic `PUT` request.
    put_cmd = 'curl -m 2 -s'
    put_cmd += ' -X PUT'
    put_cmd += ' -H  "X-metadata-token-ttl-seconds: {}"'
    put_cmd += f' {DEFAULT_IPV4}/latest/api/token'

    # Check `PUT` request fails when path is invalid.
    # Path is invalid because we remove the last character
    # at the end of the valid uri.
    _run_guest_cmd(
        ssh_connection, put_cmd[:-1].format(60),
        "Resource not found: /latest/api/toke."
    )

    # Check `PUT` request fails when token TTL is not valid.
    ttl_values = [MIN_TOKEN_TTL_SECONDS - 1, MAX_TOKEN_TTL_SECONDS + 1]
    for ttl in ttl_values:
        expected = "Invalid time to live value provided for token: {}. " \
                   "Please provide a value between {} and {}." \
            .format(ttl, MIN_TOKEN_TTL_SECONDS, MAX_TOKEN_TTL_SECONDS)
        _run_guest_cmd(ssh_connection, put_cmd.format(ttl), expected)

    # Valid `PUT` request to generate token.
    _, stdout, _ = ssh_connection.execute_command(put_cmd.format(1))
    token = stdout.read()
    assert len(token) > 0

    # Wait for token to expire.
    time.sleep(1)
    # Check `GET` request fails when expired token is provided.
    _run_guest_cmd(ssh_connection, generate_mmds_get_request(
        DEFAULT_IPV4, token=token), "MMDS token not valid.")
