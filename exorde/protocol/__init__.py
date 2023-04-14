from typing import Union

import itertools
import os, json
import logging
import asyncio
import random
import yaml
import string
import aiohttp
from pathlib import Path
from aiosow.bindings import read_only

from web3.middleware.async_cache import (
    _async_simple_cache_middleware as cache_middleware,
)
from eth_account import Account
from web3 import AsyncWeb3, AsyncHTTPProvider, Web3


def load_yaml(path):
    with open(path, "r") as _file:
        yaml_data = yaml.safe_load(_file)
        return yaml_data


async def fetch(session, url) -> Union[tuple, dict]:
    async with session.get(url) as response:
        return await response.json(content_type=None)


async def configuration():
    return {
        "configuration": load_yaml(
            os.path.dirname(os.path.abspath(__file__)) + "/configuration.yaml"
        )
    }


# cnf stands for configuration
# Data-coupling because contract instanciation requires Contract & ABIS
async def contracts_and_abi_cnf(configuration):
    async with aiohttp.ClientSession() as session:
        requests = []
        for __name__, path in configuration["ABI"].items():
            url = f"{configuration['source']}{path}"
            request = asyncio.create_task(fetch(session, url))
            requests.append(request)
        abis = await asyncio.gather(*requests)
        contracts = dict(
            await fetch(  # casted to dict for the IDEs
                session, f"{configuration['source']}/{configuration['contracts']}"
            )
        )
    logging.debug(
        "abis loaded are : %s", ", ".join([abi["contractName"] for abi in abis])
    )
    return {
        "contracts_cnf": read_only(contracts),
        "abi_cnf": read_only({abi["contractName"]: abi for abi in abis}),
    }


def instanciate_w3(url):
    w3_instance = AsyncWeb3(AsyncHTTPProvider(url))
    w3_instance.middleware_onion.add(cache_middleware)
    return w3_instance


def write_web3(configuration):
    return {
        "write_web3": instanciate_w3(
            configuration[configuration["target"]]["_urlTxSkale"]
        )
    }


def read_web3(configuration):
    return {
        "read_web3": instanciate_w3(
            random.choice(configuration[configuration["target"]]["_urlSkale"])
        )
    }


def contract(name, read_w3, abi_cnf, contracts_cnf, configuration):
    try:
        return read_w3.eth.contract(
            contracts_cnf()[configuration["target"]][name], abi=abi_cnf()[name]["abi"]
        )
    except:
        logging.debug("Skipped contract instanciation for %s", name)
        return None


def contracts(read_w3, abi_cnf, contracts_cnf, configuration):
    return {
        name: contract(name, read_w3, abi_cnf, contracts_cnf, configuration)
        for name in contracts_cnf()[configuration["target"]]
    }


# faire 2 fois
async def claim_master(user_address, user_key, AddressManager, read_web3, write_web3):
    current_nonce = await read_web3.eth.get_transaction_count(user_address)
    transaction = AddressManager.ClaimMaster().buildTransaction(
        {
            "from": user_address,
            "gasPrice": 500_000,
            "nonce": current_nonce,
        }
    )
    transaction = read_web3.eth.account.sign_transaction(transaction, user_key)
    await write_web3.eth.send_raw_transaction(transaction)


def worker_address():
    """Generates an ERC address and key"""
    keys_file = Path.home() / ".config" / "exorde" / "keys.json"
    if keys_file.exists():
        with open(keys_file, "r") as f:
            keys = json.load(f)
            logging.debug('Loaded key "%s" from file', keys["address"])
            return Account.from_key(keys["privateKey"])

    # Generate new keys if the file does not exist
    random.seed(random.random())
    base_seed = "".join(random.choices(string.ascii_uppercase + string.digits, k=256))
    acct = Account.create(base_seed)

    # Save the new keys to the file
    os.makedirs(keys_file.parent, exist_ok=True)
    with open(keys_file, "w") as f:
        keys = {
            "address": acct.address,
            "privateKey": acct.key.hex(),
        }
        json.dump(keys, f, indent=4)
        logging.debug('Saved key "%s" to file', keys["address"])

    return acct


async def log_current_rep(session, worker_address):
    async with session.get(
        "https://raw.githubusercontent.com/exorde-labs/TestnetProtocol/main/Stats/leaderboard.json"
    ) as response:
        leaderboard = json.loads(await response.text())
        logging.info(f"Current rep = {round(leaderboard.get(worker_address, 0), 4)}")


def check_erc_address_validity(erc_address):
    """check validity"""
    return Web3.is_address(erc_address)


# erc_address = Web3.to_checksum_address(erc_address)
def check_provided_user_address(user_address):
    if not check_erc_address_validity(user_address):
        logging.critical("Invalid user address")
    else:
        logging.info("User address %s is valid : %s", user_address)


def sign_transaction(transaction, worker_key, read_web3):
    return read_web3.eth.account.sign_transaction(transaction, worker_key)


async def send_raw_transaction(transaction, write_web3, read_web3, worker_address):
    try:
        previous_nonce = await read_web3.eth.get_transaction_count(worker_address)
        transaction_hash = await write_web3.eth.send_raw_transaction(
            transaction.rawTransaction
        )
        logging.info("A transaction has been sent")
        logging.info("Waiting for transaction confirmation")
        for i in range(10):
            sleep_time = i * 1.5 + 1
            logging.debug(
                f"Waiting {sleep_time} seconds for faucet transaction confirmation"
            )
            await asyncio.sleep(sleep_time)
            # wait for new nounce by reading proxy
            current_nounce = await read_web3.eth.get_transaction_count(worker_address)
            if current_nounce > previous_nonce:
                # found a new transaction because account nounce has increased
                break
        await read_web3.eth.wait_for_transaction_receipt(
            transaction_hash, timeout=120, poll_latency=20
        )
    except Exception as e:
        logging.error(f"Error sending transaction : {e}")
    logging.info("A transaction has been confirmed")
    return {"transaction": None, "current_cid_commit": None}


async def nonce(worker_address, read_web3):
    try:
        return await read_web3.eth.get_transaction_count(worker_address)
    except Exception:
        logging.error(f"Error getting nonce ({worker_address})")


async def spot_data(cid, DataSpotting):
    # todo: "1" is for twitter.com, it should be specified dynamicaly
    try:
        return DataSpotting.functions.SpotData([cid], ["1"], [100], "")
    except Exception:
        logging.error("Error calling SpotData")


def rotate_gateways():
    gateways = [
        "http://ipfs-gateway.exorde.network/ipfs/",
        "http://ipfs-gateway.exorde.network/ipfs/",
        "http://ipfs-gateway.exorde.network/ipfs/",
        "https://w3s.link/ipfs/",
        "https://ipfs.io/ipfs/",
        "https://ipfs.eth.aragon.network/ipfs/",
        "https://api.ipfsbrowser.com/ipfs/get.php?hash=",
    ]

    return (gateways[i % len(gateways)] for i in itertools.count())


async def is_new_work_available(worker_address, DataSpotting) -> bool:
    return await DataSpotting.IsNewWorkAvailable(worker_address).call()


async def get_current_work(worker_address, DataSpotting) -> int:
    return await DataSpotting.GetCurrentWork(worker_address)


async def get_ipfs_hashes_for_batch(DataSpotting, batch_id) -> list:
    return await DataSpotting.getIPFShashesForBatch(batch_id).call()


async def build_transaction(transaction, worker_address, nonce):
    try:
        return await transaction.build_transaction(
            {"nonce": nonce, "from": worker_address, "gasPrice": 100_000}
        )
    except Exception:
        logging.error("Error building transaction")


async def init_gas_cache():
    return {"gas_cache": {}}


async def get_balance(read_web3, worker_address):
    try:
        return await read_web3.eth.get_balance(worker_address)
    except Exception as e:
        logging.error(e)
        return 0


def select_random_faucet():
    private_key_base = "deaddeaddeaddead5fb92d83ed54c0ea1eb74e72a84ef980d42953caaa6d"
    ## faucets private keys are ["Private_key_base"+("%0.4x" % i)] with i from 0 to 499. Last 2 bytes is the selector.

    selected_faucet_index = random.randrange(
        0, 499 + 1, 1
    )  # [select index between 0 & 499 (500 faucets)]

    hex_selector_bytes = "%0.4x" % selected_faucet_index
    faucet_private_key = private_key_base + hex_selector_bytes
    return selected_faucet_index, faucet_private_key


async def faucet(__balance__, write_web3, read_web3, selected_faucet, worker_address):
    if not Web3.is_address(worker_address):
        logging.critical("Invalid worker address")
        os._exit(1)
    logging.info(f"Faucet with '{selected_faucet} and {worker_address}")
    faucet_address = read_web3.eth.account.from_key(selected_faucet[1]).address
    previous_nounce = await read_web3.eth.get_transaction_count(faucet_address)
    signed_transaction = read_web3.eth.account.sign_transaction(
        {
            "nonce": previous_nounce,
            "gasPrice": 500_000,
            "gas": 100_000,
            "to": worker_address,
            "value": 500000000000000,
            "data": b"Hi Exorde!",
        },
        selected_faucet[1],
    )
    transaction_hash = await write_web3.eth.send_raw_transaction(
        signed_transaction.rawTransaction
    )

    await asyncio.sleep(3)
    logging.info("Waiting for transaction confirmation")
    for i in range(10):
        sleep_time = i * 1.5 + 1
        logging.debug(
            f"Waiting {sleep_time} seconds for faucet transaction confirmation"
        )
        await asyncio.sleep(sleep_time)
        # wait for new nounce by reading proxy
        current_nounce = await read_web3.eth.get_transaction_count(faucet_address)
        if current_nounce > previous_nounce:
            # found a new transaction because account nounce has increased
            break

    transaction_receipt = await read_web3.eth.wait_for_transaction_receipt(
        transaction_hash, timeout=120, poll_latency=20
    )
    logging.info(
        f"SFUEL funding transaction {transaction_receipt.transactionHash.hex()}"
    )


async def estimate_gas(transaction, read_web3, gas_cache):
    async def do_estimate_gas():
        gas = 100_000  # default gas amount
        estimate = await read_web3.eth.estimate_gas(transaction) * 1.5
        if estimate < 100_000:
            gas = estimate + 500_000
        return max(10_000_000, gas)

    function_bytecode = str(transaction["data"])[2:10]
    data_length = len(str(transaction["data"]))
    gas_key = (function_bytecode, data_length)
    if gas_key in gas_cache:
        transaction["gas"] = gas_cache[gas_key]
    else:
        estimate = await do_estimate_gas()
        gas_cache[gas_key] = estimate
        transaction["gas"] = gas_cache[gas_key]
    return transaction
