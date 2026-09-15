import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

import ipfs_api
import requests
import yaml
from ipfs_remote.ipfshttpclient2.exceptions import ConnectionError
from nacl.secret import SecretBox
from robonomicsinterface import Account, Datalog
from substrateinterface import Keypair, KeypairType, SubstrateInterface
from substrateinterface.utils.ss58 import is_valid_ss58_address

LOGGER = logging.getLogger(__name__)
# Step-by-step status messages; hidden with --quiet.
PROGRESS = logging.getLogger(__name__ + ".progress")

CREDS_FILE = "creds.yaml"
DOWNLOAD_ATTEMPTS = 5
DOWNLOAD_BACKOFF_SECONDS = 3
DOWNLOAD_TIMEOUT_SECONDS = (10, 60)  # (connect, read between chunks)
RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}
PROGRESS_INTERVAL_SECONDS = 2.0
DEFAULT_PASS_VAULT = "Report Service"
PASS_ITEM_PREFIX = "Robonomics - "


@dataclass(frozen=True)
class SenderConfig:
    address: str
    name: str


@dataclass(frozen=True)
class AppConfig:
    recipient_address: str
    pass_vault: str
    sender_addresses: list[SenderConfig]
    reports_dir: Path
    reports_per_address: int
    ipfs_gateway: str
    network_wss: str
    clean_reports: bool


def setup_logging(quiet: bool) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if quiet:
        PROGRESS.setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download and decrypt recent Home Assistant reports "
            "from Robonomics datalog."
        )
    )
    parser.add_argument("--creds", default=CREDS_FILE, help="Path to creds YAML file.")
    parser.add_argument(
        "--quiet", action="store_true", help="Hide step-by-step progress messages."
    )
    return parser.parse_args()


def format_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB"):
        if num_bytes < 1024:
            return (
                f"{num_bytes:.0f} {unit}" if unit == "B" else f"{num_bytes:.1f} {unit}"
            )
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"


def require_config_value(creds: dict, key: str):
    value = creds.get(key)
    if value is None:
        raise ValueError(f"{key} is required.")
    return value


def require_config_str(creds: dict, key: str) -> str:
    value = str(require_config_value(creds, key)).strip()
    if not value:
        raise ValueError(f"{key} cannot be empty.")
    return value


def require_config_int(creds: dict, key: str) -> int:
    value = require_config_value(creds, key)
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{key} must be an integer.") from e


def require_config_bool(creds: dict, key: str) -> bool:
    value = require_config_value(creds, key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false.")
    return value


def normalize_sender_addresses(creds: dict) -> list[SenderConfig]:
    senders = creds.get("sender_addresses")
    if senders is None:
        raise ValueError("sender_addresses is required.")

    result: list[SenderConfig] = []
    for item in senders:
        if isinstance(item, str):
            address = item.strip()
            name = address
        elif isinstance(item, dict):
            address = str(item.get("address") or "").strip()
            name = str(item.get("name") or address).strip()
        else:
            raise ValueError("Each sender address must be a string or a mapping.")

        if not address:
            raise ValueError("Sender address cannot be empty.")
        if not is_valid_ss58_address(address):
            raise ValueError(f"Sender '{name}' has an invalid SS58 address: {address}")
        result.append(SenderConfig(address=address, name=name or address))

    if not result:
        raise ValueError("No sender addresses configured.")
    return result


def load_config(args: argparse.Namespace) -> AppConfig:
    creds_path = Path(args.creds)
    with open(creds_path, encoding="utf-8") as f:
        creds: dict = yaml.load(f, Loader=yaml.SafeLoader) or {}

    if "recipient_seed" in creds:
        raise ValueError(
            "recipient_seed is no longer read from creds.yaml: store the seed in "
            "Proton Pass and set recipient_address instead (see README)."
        )

    reports_per_address = require_config_int(creds, "reports_per_address")
    if reports_per_address < 1:
        raise ValueError("reports_per_address must be greater than 0.")

    return AppConfig(
        recipient_address=require_config_str(creds, "recipient_address"),
        pass_vault=str(creds.get("pass_vault") or DEFAULT_PASS_VAULT).strip(),
        sender_addresses=normalize_sender_addresses(creds),
        reports_dir=Path(require_config_str(creds, "reports_dir")),
        reports_per_address=reports_per_address,
        ipfs_gateway=require_config_str(creds, "ipfs_gateway"),
        network_wss=require_config_str(creds, "network_wss"),
        clean_reports=require_config_bool(creds, "clean_reports"),
    )


def load_recipient_seed(address: str, vault: str) -> str:
    """Fetch the recipient seed from Proton Pass; it is kept in memory only."""
    item_title = PASS_ITEM_PREFIX + address
    env = os.environ.copy()
    # Required by pass-cli when running under an agent token.
    env.setdefault("PROTON_PASS_AGENT_REASON", "Decrypt Home Assistant reports")
    try:
        result = subprocess.run(
            [
                "pass-cli",
                "item",
                "view",
                "--vault-name",
                vault,
                "--item-title",
                item_title,
                "--field",
                "seed",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
    except FileNotFoundError as e:
        raise ValueError("pass-cli is not installed.") from e

    seed = result.stdout.strip()
    if result.returncode != 0 or not seed:
        raise ValueError(
            f"Cannot read field 'seed' of item '{item_title}' in vault '{vault}'. "
            "Check `pass-cli login` and that the item exists."
        )
    return seed


def ipfs_download(cid: str, file_path: Path, gateway: str) -> None:
    """
    Function for download files from IPFS
    :param cid: File CID
    :param file_path: Full path for saving file
    :param gateway: IPFS gateway to download file
    :return: None
    """
    if gateway:
        url: str = urllib.parse.urljoin(gateway.rstrip("/") + "/", "ipfs/" + cid)
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            PROGRESS.info("GET %s (attempt %s/%s)", url, attempt, DOWNLOAD_ATTEMPTS)
            try:
                stream_to_file(url, file_path)
                return
            except requests.RequestException as e:
                status = e.response.status_code if e.response is not None else None
                retryable = status is None or status in RETRYABLE_HTTP_STATUSES
                if not retryable or attempt == DOWNLOAD_ATTEMPTS:
                    LOGGER.warning("IPFS gateway download failed: %s", e)
                    break
                delay = DOWNLOAD_BACKOFF_SECONDS * 2 ** (attempt - 1)
                PROGRESS.info(
                    "Attempt %s failed (%s), retrying in %ss", attempt, e, delay
                )
                time.sleep(delay)

    LOGGER.warning(
        "Falling back to a local IPFS node for %s; this can take a long time", cid
    )
    ipfs_api.download(cid, str(file_path))


def stream_to_file(url: str, file_path: Path) -> None:
    started = time.monotonic()
    with requests.get(
        url, stream=True, allow_redirects=True, timeout=DOWNLOAD_TIMEOUT_SECONDS
    ) as response:
        response.raise_for_status()
        total = int(response.headers.get("Content-Length") or 0)
        PROGRESS.info(
            "Gateway responded after %.1fs, size %s",
            time.monotonic() - started,
            format_size(total) if total else "unknown",
        )
        received = 0
        last_report = time.monotonic()
        with open(file_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                f.write(chunk)
                received += len(chunk)
                now = time.monotonic()
                if now - last_report >= PROGRESS_INTERVAL_SECONDS:
                    PROGRESS.info(
                        "  %s%s, %s/s",
                        format_size(received),
                        f" of {format_size(total)}" if total else "",
                        format_size(received / (now - started)),
                    )
                    last_report = now
    PROGRESS.info(
        "Downloaded %s in %.1fs", format_size(received), time.monotonic() - started
    )


def parse_decrypted(text: str) -> tuple[str, dict | None]:
    """
    Parse decrypted data if metadata was added or return just data otherwise.
    """
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "payload" in obj:
            meta = obj.get("meta")
            return obj["payload"], meta if isinstance(meta, dict) else None
    except json.JSONDecodeError:
        pass
    return text, None


def clean_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for entry in path.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def clean_reports_dir(path: Path) -> None:
    resolved = path.resolve()
    unsafe_paths = {Path.cwd().resolve(), Path.home().resolve(), Path("/").resolve()}
    if resolved in unsafe_paths:
        raise ValueError(f"Refusing to clean unsafe reports directory: {resolved}")
    clean_dir(path)


def safe_path_part(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return value.strip("._-") or "unknown"


def safe_output_path(base_dir: Path, file_name: str) -> Path:
    if not file_name:
        file_name = "decrypted_report.txt"

    candidate = (base_dir / file_name).resolve()
    base_resolved = base_dir.resolve()
    if not candidate.is_relative_to(base_resolved):
        candidate = base_resolved / Path(file_name).name
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


def safe_extract_zip(zip_path: Path, target_dir: Path) -> None:
    target_resolved = target_dir.resolve()
    with ZipFile(zip_path, "r") as zip_file:
        for member in zip_file.infolist():
            member_path = (target_resolved / member.filename).resolve()
            if not member_path.is_relative_to(target_resolved):
                raise ValueError(f"Unsafe path in archive: {member.filename}")
        zip_file.extractall(target_resolved)


def decrypt_msg(
    encrypted_msg: str,
    sender_public_key: bytes,
    recipient_keypair: Keypair,
) -> bytes:
    """
    Decrypt message with recipient private key and sender public key.

    :param encrypted_msg:       Message to decrypt
    :param sender_public_key:   Sender public key
    :param recipient_keypair:   Recipient account keypair

    :return: Decrypted message
    """
    if encrypted_msg[:2] == "0x":
        encrypted_msg = encrypted_msg[2:]

    bytes_encrypted = bytes.fromhex(encrypted_msg)

    return recipient_keypair.decrypt_message(bytes_encrypted, sender_public_key)


def multi_envelope_decrypt_data(
    encryption_package: str,
    recipient_account: Account,
    sender_address: str,
) -> str:
    """
    Decrypt JSON structure with encrypted data: first decrypt the secret key
    (asymmetric), then use the secret key to decrypt the data itself.

    :return: data after decryption
    """

    try:
        package_json = json.loads(encryption_package)
    except json.JSONDecodeError as e:
        LOGGER.warning("Envelope decrypt: invalid JSON package")
        raise ValueError("Invalid encryption package JSON") from e

    try:
        encrypted_secret_keys = package_json["keys"]
        encrypted_data_hex = package_json["data"]
    except (KeyError, TypeError, ValueError) as e:
        LOGGER.warning("Envelope decrypt: missing required fields in package")
        raise ValueError("Invalid encryption package structure") from e

    recipient_address = recipient_account.get_address()
    encrypted_secret_key = encrypted_secret_keys.get(recipient_address)
    if not encrypted_secret_key:
        LOGGER.warning(
            "Envelope decrypt: recipient key not found for %s", recipient_address
        )
        raise ValueError("Recipient is not authorized for this package")

    try:
        sender_kp = Keypair(
            ss58_address=sender_address, crypto_type=KeypairType.ED25519
        )

        secret_key = decrypt_msg(
            encrypted_secret_key, sender_kp.public_key, recipient_account.keypair
        )
    except Exception as e:
        LOGGER.warning("Envelope decrypt: failed to unwrap secret key")
        raise ValueError("Failed to decrypt secret key") from e

    try:
        if encrypted_data_hex[:2] == "0x":
            encrypted_data_hex = encrypted_data_hex[2:]
        encrypted_data = bytes.fromhex(encrypted_data_hex)

        decrypted_data_bytes = SecretBox(secret_key).decrypt(encrypted_data)
        decrypted_data = decrypted_data_bytes.decode("utf-8")

        return decrypted_data
    except Exception as e:
        LOGGER.warning("Envelope decrypt: failed to decrypt payload")
        raise ValueError("Failed to decrypt payload") from e


def get_datalog_item(
    datalog: Datalog, sender_address: str, index: int
) -> tuple[int, str] | None:
    record = datalog._service_functions.chainstate_query(
        "Datalog", "DatalogItem", [sender_address, index]
    )
    if not record or record[0] == 0:
        return None
    return record


def read_datalog_window_size(network_wss: str) -> int:
    with SubstrateInterface(url=network_wss) as substrate:
        return int(substrate.get_constant("Datalog", "WindowSize").value)


def ring_buffer_indices(start: int, end: int, window_size: int) -> list[int]:
    """Datalog slots from oldest to newest.

    The pallet keeps the last `window_size - 1` records per account and reuses
    slots once full, so `end < start` means the buffer has wrapped around.
    """
    count = end - start if start <= end else window_size + end - start
    return [(start + offset) % window_size for offset in range(count)]


def get_recent_datalogs(
    datalog: Datalog, sender_address: str, count: int, window_size: int
) -> list[tuple[int, int, str]]:
    PROGRESS.info("Reading datalog index")
    index_info = datalog.get_index(sender_address)
    start = int(index_info["start"])
    end = int(index_info["end"])
    all_indices = ring_buffer_indices(start, end, window_size)
    PROGRESS.info(
        "Datalog ring buffer start=%s end=%s holds %s record(s)%s",
        start,
        end,
        len(all_indices),
        " (wrapped)" if end < start else "",
    )

    indices = all_indices[-count:]
    result: list[tuple[int, int, str]] = []
    for index in indices:
        PROGRESS.info("Reading datalog #%s", index)
        record = get_datalog_item(datalog, sender_address, index)
        if record is None:
            LOGGER.warning("Datalog #%s is empty for %s", index, sender_address)
            continue
        timestamp, datalog_content = record
        result.append((index, timestamp, str(datalog_content)))
    return result


def decrypt_archive_files(
    archive_files_path: Path,
    output_dir: Path,
    recipient_account: Account,
    sender_address: str,
) -> int:
    decrypted_count = 0
    entries = [e for e in sorted(archive_files_path.rglob("*")) if e.is_file()]
    for position, entry in enumerate(entries, start=1):
        PROGRESS.info(
            "Decrypting %s/%s: %s (%s)",
            position,
            len(entries),
            entry.name,
            format_size(entry.stat().st_size),
        )
        with open(entry, encoding="utf-8") as f:
            data = f.read()

        try:
            decrypted_data = multi_envelope_decrypt_data(
                data, recipient_account, sender_address
            )

            decrypted_payload, decrypted_meta = parse_decrypted(decrypted_data)
            file_name = (
                decrypted_meta.get("orig_file_name")
                if decrypted_meta
                else entry.with_suffix(".txt").name
            )

            file_path = safe_output_path(output_dir, file_name)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(decrypted_payload)
            decrypted_count += 1
            LOGGER.info("File %s is successfully decrypted", file_path)
        except Exception as e:
            LOGGER.warning("Problem during decrypting %s: %s", entry.name, e)
    return decrypted_count


def process_datalog_report(
    cid: str,
    sender_address: str,
    output_dir: Path,
    recipient_account: Account,
    ipfs_gateway: str,
) -> int:
    archive_path = output_dir / f"{safe_path_part(cid)}.zip"

    try:
        LOGGER.info("Downloading IPFS archive %s", cid)
        ipfs_download(cid, archive_path, ipfs_gateway)

        with TemporaryDirectory(prefix="encrypted_logs_", dir=output_dir) as tmp_dir:
            archive_files_path = Path(tmp_dir)
            safe_extract_zip(archive_path, archive_files_path)

            return decrypt_archive_files(
                archive_files_path, output_dir, recipient_account, sender_address
            )
    finally:
        archive_path.unlink(missing_ok=True)


def process_sender(
    sender: SenderConfig,
    datalog: Datalog,
    recipient_account: Account,
    config: AppConfig,
    window_size: int,
) -> None:
    sender_dir = config.reports_dir / safe_path_part(sender.name)
    sender_dir.mkdir(parents=True, exist_ok=True)

    LOGGER.info("Processing sender %s (%s)", sender.name, sender.address)
    started = time.monotonic()
    records = get_recent_datalogs(
        datalog, sender.address, config.reports_per_address, window_size
    )
    if not records:
        LOGGER.warning("No datalog records found for %s", sender.address)
        return

    for position, (index, timestamp, cid) in enumerate(records, start=1):
        PROGRESS.info(
            "Report %s/%s: datalog #%s, CID %s", position, len(records), index, cid
        )
        report_dir = sender_dir / f"datalog_{index}_{timestamp}"
        report_dir.mkdir(parents=True, exist_ok=True)
        cid_file = report_dir / "cid.txt"
        with open(cid_file, "w", encoding="utf-8") as f:
            f.write(cid + "\n")

        try:
            decrypted_count = process_datalog_report(
                cid,
                sender.address,
                report_dir,
                recipient_account,
                config.ipfs_gateway,
            )
            LOGGER.info(
                "Datalog #%s from %s: decrypted %s file(s)",
                index,
                sender.address,
                decrypted_count,
            )
        except (ConnectionError, OSError, ValueError) as e:
            LOGGER.warning(
                "Problem during processing datalog #%s from %s: %s",
                index,
                sender.address,
                e,
            )
    PROGRESS.info("Sender %s done in %.1fs", sender.name, time.monotonic() - started)


def main() -> int:
    args = parse_args()
    setup_logging(args.quiet)
    started = time.monotonic()
    try:
        config = load_config(args)

        PROGRESS.info(
            "Reading recipient seed from Proton Pass (vault '%s')", config.pass_vault
        )
        recipient_account = Account(
            load_recipient_seed(config.recipient_address, config.pass_vault),
            crypto_type=KeypairType.ED25519,
            remote_ws=config.network_wss,
        )
        if recipient_account.get_address() != config.recipient_address:
            raise ValueError(
                "Seed from Proton Pass does not match recipient_address "
                f"{config.recipient_address}."
            )
        PROGRESS.info("Recipient address verified: %s", config.recipient_address)
        PROGRESS.info("Using Robonomics node %s", config.network_wss)
        window_size = read_datalog_window_size(config.network_wss)
        PROGRESS.info("Datalog window size: %s", window_size)

        if config.clean_reports:
            clean_reports_dir(config.reports_dir)
        else:
            config.reports_dir.mkdir(parents=True, exist_ok=True)

        datalog = Datalog(
            recipient_account, rws_sub_owner=recipient_account.get_address()
        )

        for sender in config.sender_addresses:
            try:
                process_sender(sender, datalog, recipient_account, config, window_size)
            except Exception as e:
                LOGGER.warning(
                    "Problem during processing sender %s: %s", sender.address, e
                )

        LOGGER.info("All done in %.1fs", time.monotonic() - started)
        return 0
    except Exception as e:
        LOGGER.error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
