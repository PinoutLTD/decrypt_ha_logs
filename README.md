# decrypt_ha_logs — archived, superseded by rrs-connector

This was the hand tool for reading a report while the connector was being
built. [`rrs-connector`](https://github.com/PinoutLTD/rrs-connector) now does
the same thing and more, so this repository is archived and no longer
maintained:

```bash
rrs-connector --command fetch --sender <ss58> --cid <cid> --output ./case
rrs-connector --command fetch --sender <ss58> --last 2
```

Like this tool, `fetch` leaves no trace in the service's state: it downloads,
decrypts, and writes where it is told. Unlike this tool, it shares one
implementation with the service that files the reports as helpdesk tickets,
so the report format has a single reader.

The history stays readable here — including the ring buffer fix, which is the
reason the connector reads active sites correctly.

---

Downloads recent Home Assistant report archives from Robonomics datalog records,
decrypts them, and stores the decrypted files in a directory tree that is easy
to inspect.

## Configuration

Copy `creds.yaml.example` to `creds.yaml` and fill in the recipient address and
sender addresses. The config contains no secrets.

```yaml
recipient_address: "4..."
pass_vault: Report Service

sender_addresses:
  - address: "..."
    name: "home-a"
  - address: "..."
    name: "home-b"

reports_per_address: 2
reports_dir: reports
clean_reports: true
ipfs_gateway: https://gateway.pinata.cloud
network_wss: wss://polkadot.rpc.robonomics.network/
```

### Recipient seed

The seed never lives on disk. It is read at startup with
[`pass-cli`](https://protonpass.github.io/pass-cli/) from the vault `pass_vault`,
item titled `Robonomics - <recipient_address>`, field `seed`, and the script
checks that the seed derives exactly `recipient_address` before doing anything.
Log in once with `pass-cli login`. When running under a Proton Pass agent token,
`PROTON_PASS_AGENT_REASON` may be overridden in the environment.

## Installation

Create a virtual environment in the project directory:

```bash
python3 -m venv .venv
```

Activate it:

```bash
source .venv/bin/activate
```

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

## Usage

```bash
.venv/bin/python decrypt_ha_logs.py
```

Use another config file:

```bash
.venv/bin/python decrypt_ha_logs.py --creds /path/to/creds.yaml
```

The script prints step-by-step progress (datalog reads, download speed, retries,
decryption). Hide it with `--quiet`.

Reports are pinned on Pinata, so `https://gateway.pinata.cloud` serves fresh CIDs
reliably; public gateways such as `ipfs.io` first have to find the content in the
IPFS network and can stall on a cold CID.

By default, `reports_dir` is cleaned before every run. Decrypted reports are
stored like this:

```text
reports/
  home-a/
    datalog_16_1710000000/
      cid.txt
      home-assistant.log
      issue_description.json
      trace.saved_traces
```
