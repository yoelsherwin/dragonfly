import pytest
import asyncio
import redis
from redis import asyncio as aioredis
from . import DflyInstanceFactory, dfly_args
import logging
import time
import subprocess
#from replication_test import check_all_replicas_finished
#import replication_test
from dragonfly.replication_test import check_all_replicas_finished

BASE_PORT = 1111
ADMIN_PORT = 1211

"""
Test replication with tls and non-tls options
"""

# 1. Number of master threads
# 2. Number of threads for each replica
replication_cases = [(8, 8)]

tls_server_key_file_name = "df-key.pem"
tls_server_cert_file_name = "df-cert.pem"

def tls_args(df_instance):
    with_tls_args = {"tls": "",
                    "tls_key_file": df_instance.dfly_path + tls_server_key_file_name,
                    "tls_cert_file": df_instance.dfly_path + tls_server_cert_file_name,
                    "no_tls_on_admin_port": "true"}
    return with_tls_args

def gen_tls_cert(df_instance):
    # We first need to generate the tls certificates to be used by the server

    # Step 1
    # Generate CA (certificate authority) key and self-signed certificate
    # In production, CA should be generated by a third party authority
    # Expires in one day and is not encrtypted (-nodes)
    # X.509 format for the key
    ca_key = df_instance.dfly_path + "ca-key.pem"
    ca_cert = df_instance.dfly_path + "ca-cert.pem"
    step1 = rf'openssl req -x509 -newkey rsa:4096 -days 1 -nodes -keyout {ca_key} -out {ca_cert} -subj "/C=GR/ST=SKG/L=Thessaloniki/O=KK/OU=AcmeStudios/CN=Gr/emailAddress=acme@gmail.com"'
    subprocess.run(step1, shell=True)

    # Step 2
    # Generate Dragonfly's private key and certificate signing request (CSR)
    tls_server_key = df_instance.dfly_path + tls_server_key_file_name
    tls_server_req = df_instance.dfly_path + "df-req.pem"
    step2 = rf'openssl req -newkey rsa:4096 -nodes -keyout {tls_server_key} -out {tls_server_req} -subj "/C=GR/ST=SKG/L=Thessaloniki/O=KK/OU=Comp/CN=Gr/emailAddress=does_not_exist@gmail.com"'
    subprocess.run(step2, shell=True)

    # Step 3
    # Use CA's private key to sign dragonfly's CSR and get back the signed certificate
    tls_server_cert = df_instance.dfly_path + tls_server_cert_file_name;
    step3 = fr'openssl x509 -req -in {tls_server_req} -days 1 -CA {ca_cert} -CAkey {ca_key} -CAcreateserial -out {tls_server_cert}'
    subprocess.run(step3, shell=True)

@pytest.mark.asyncio
@pytest.mark.parametrize("t_master, t_replica", replication_cases)
@dfly_args({"dbfilename": "test-tls-replication-{{timestamp}}"})
async def test_replication_all(df_local_factory, df_seeder_factory, t_master, t_replica):
    # 1. Spin up dragonfly without tls, debug populate and then shut it down
    master = df_local_factory.create(port=BASE_PORT, proactor_threads=t_master)
    master.start()
    c_master = aioredis.Redis(port=master.port)
    await c_master.execute_command("DEBUG POPULATE 100")
    await c_master.execute_command("SAVE")
    db_size = await c_master.execute_command("DBSIZE")
    assert 100 == db_size

    master.stop()
    with_tls_args = tls_args(master)
    gen_tls_cert(master)
    # 2. Spin up dragonfly again, this time, with different options:
    #   a. Enable tls
    #   b. Allow non-tls connection from replica to master
    master = df_local_factory.create(admin_port=ADMIN_PORT, admin_nopass=True, **with_tls_args, port=BASE_PORT, proactor_threads=t_master)
    master.start()
    # we need to sleep, because currently tls connections on master fail
    # TODO Fix this once master properly loads tls certificates and redis-cli does not
    # require --insecure flag, therefore for now we can't use `wait_available`
    time.sleep(10)

    # 3. Try to connect on master. This should fail.
    c_master = aioredis.Redis(port=master.port)
    try:
        await c_master.execute_command("DBSIZE")
        raise "Non tls connection connected on master with tls. This should NOT happen"
    except redis.ConnectionError:
        pass

    # 4. Try with admin port.
    c_master = aioredis.Redis(port=ADMIN_PORT)
    db_size = await c_master.execute_command("DBSIZE")
    assert 100 == db_size

    # 5. Spin up a replica and initiate a REPLICAOF
    replica = df_local_factory.create(port=BASE_PORT + 1, proactor_threads=t_replica,)
    replica.start()
    c_replica = aioredis.Redis(port=replica.port)
    res = await c_replica.execute_command("REPLICAOF localhost " + str(ADMIN_PORT))
    assert b"OK" == res
    await check_all_replicas_finished([c_replica], c_master)

    # 6. Verify that replica dbsize == debug populate key size -- replication works
    db_size = await c_replica.execute_command("DBSIZE")
    assert 100 == db_size
