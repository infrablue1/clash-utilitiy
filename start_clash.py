import os
import logging
import sys
import httpx
from pathlib import Path
import subprocess
import signal
import argparse
from ruamel.yaml import YAML
from datetime import datetime

logger: logging.Logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.propagate = False

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(handler)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--refresh-config",
        action="store_true",
        default=False,
        help="refresh clash config file",
    )

    parser.add_argument("--clash-url", default="", help="clash subscribe url")
    parser.add_argument("--clash-secret", default="", help=" subscribe secret")
    parser.add_argument(
        "--admin",
        action="store_true",
        default=False,
        help="use sudo or admin to start clash-core",
    )
    parser.add_argument("--log", default="", help="clash log output file")
    parser.add_argument(
        "--stop",
        action="store_true",
        default=False,
        help="stop background clash",
    )

    args = parser.parse_args()

    if args.refresh_config and not args.clash_url:
        parser.error("--refresh-config requires --clash-url")

    return args


#'''
def check_url(url: str, retries: int, timeout_config: httpx.Timeout) -> bool:
    limits = httpx.Limits(max_keepalive_connections=1, max_connections=1)

    for attempt in range(retries):
        try:
            with httpx.Client(
                verify=False,
                follow_redirects=True,
                timeout=timeout_config,
                limits=limits,
            ) as client:
                response = client.get(url)
                logger.info(f"attempt {attempt + 1}: status={response.status_code}")
                return 200 <= response.status_code < 400
        except httpx.TimeoutException as e:
            logger.warning(f"attempt {attempt + 1} timeout: {e}")
        except httpx.RequestError as e:
            logger.warning(f"attempt {attempt + 1} error: {type(e).__name__}: {e}")

    return False


def download_file(
    url: str, dest: Path, retries: int, timeout_config: httpx.Timeout
) -> bool:
    for attempt in range(retries):
        try:
            with httpx.Client(
                verify=False,
                follow_redirects=True,
                timeout=timeout_config,
            ) as client:
                with client.stream("GET", url) as response:
                    response.raise_for_status()
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with open(dest, "wb") as f:
                        for chunk in response.iter_bytes(chunk_size=8192):
                            f.write(chunk)
                    return True

        except httpx.TimeoutException as e:
            logger.warning(f"attempt {attempt + 1} timeout: {e}")
        except httpx.HTTPStatusError as e:
            logger.warning(
                f"attempt {attempt + 1} bad status: {e.response.status_code}"
            )
        except httpx.RequestError as e:
            logger.warning(f"attempt {attempt + 1} error: {type(e).__name__}: {e}")
    return False


def start_clash(
    clash_core: Path,
    config_path: Path,
    clash_config: Path,
    log_file: Path,
    use_sudo: bool,
):
    cmd = [clash_core, "-d", config_path]
    if use_sudo:
        cmd = ["sudo"] + cmd
    cmd_str = " ".join([str(item) for item in cmd])
    logger.info(f"Running clash-core with '{cmd_str}'")
    logger.info(f"Clash log is in {log_file}")
    f = open(log_file, "a")
    process = subprocess.Popen([clash_core, "-d", config_path], stdout=f, stderr=f)
    logger.info(f"Clash started in background pid: {process.pid}")

    # Read config file
    yaml = YAML()
    config_dict: dict = {}
    with open(clash_config, "r", encoding="utf-8") as f:
        config_dict = yaml.load(f)

    secret = config_dict.get("secret")
    port = config_dict.get("port")
    sock_port = config_dict.get("sock_port")
    mode = config_dict.get("mode")
    restful_port = 9090

    logger.info(f"secret is {secret}")
    logger.info(f"current rule is {mode}")
    logger.info(f"HTTP proxy listening at: [::]:{port}")
    logger.info(f"SOCKS proxy listening at: [::]:{sock_port}")
    logger.info(f"RESTful API listening at: [::]:{restful_port}")


def stop_clash(clash_core: Path, use_sudo: bool):
    process_name = clash_core.name
    logger.info(f"Stopping clash process:: {process_name}")

    cmd = ["pkill", "-f", process_name]
    if use_sudo:
        cmd = ["sudo"] + cmd

    result = subprocess.run(cmd)
    if result.returncode == 0:
        logger.info("Clash stopped")
    elif result.returncode == 1:
        logger.info("No matching clash process found")
    else:
        logger.error(f"pkill failed with return code {result.returncode}")


def prepare_clash_config(
    clash_url: str,
    clash_secret: str,
    template_config: Path,
    proxies: Path,
    header_config: Path,
    clash_config: Path,
):

    arch = None
    if sys.platform == "win32":
        arch = "win"
    elif sys.platform.startswith("linux"):
        arch = "linux"
    else:
        logger.fatal("Now it only supports linux and win.")
        sys.exit(1)

    timeout = 10.0
    timeout_config = httpx.Timeout(
        connect=timeout, read=timeout, write=timeout, pool=timeout
    )
    retries = 5

    # Checking whether clash url is valid
    logger.info(f"Checking whether clash URL is valid...")
    url_valid = check_url(clash_url, retries, timeout_config)
    if not url_valid:
        logger.info("CLASH URL is not valid!")
        sys.exit(1)

    # Downloading config.yaml
    logger.info("Downloading clash config file...")

    download_success = download_file(
        clash_url, template_config, retries, timeout_config
    )
    if not download_success:
        logging.info("Downloading clash config file failed!")
        sys.exit(1)

    logger.info(f"Downloaded clash config file in {template_config}")

    # Generate new config file
    # Extract proxies
    lines = template_config.read_text().splitlines(keepends=True)
    start = next(
        (i for i, line in enumerate(lines) if line.startswith("proxies:")), None
    )
    if start is None:
        raise ValueError("'proxies:' section not found")
    proxies.write_text("".join(lines[start:]))
    logger.info(f"Extract proxies to {proxies}")

    yaml = YAML()
    yaml.preserve_quotes = True

    with open(clash_config, "w") as dest:
        with open(header_config, "r", encoding="utf-8") as f:
            header = yaml.load(f)
        header["secret"] = clash_secret
        yaml.dump(header, dest)
        with open(proxies, "r") as f:
            dest.write(f.read())
    logger.info(f"Generate clash config to {clash_config}")


def main():
    args = parse_args()

    # Setting file path
    project_path = Path(os.getcwd())
    logger.info(f"current path is {project_path}")

    # template/
    template_path = project_path / "template"
    template_config = template_path / "clash.yaml"
    header_config = template_path / "template_config.yaml"
    proxies = template_path / "proxies.txt"

    # config/
    config_path = project_path / "config"
    clash_config = config_path / "config.yaml"

    # log
    log_path = project_path / "log"
    default_log = log_path / "log.txt"

    # clash bin
    clash_core = project_path / "bin" / "clash-linux-amd64"

    # Get clash url from env

    if args.refresh_config:
        prepare_clash_config(
            args.clash_url,
            args.clash_secret,
            template_config,
            proxies,
            header_config,
            clash_config,
        )

    # Start clash servie
    log_filename = args.log
    log_file = Path(log_filename) if args.log else default_log

    if args.stop:
        logger.info(f"Stopping clash service")
        stop_clash(clash_core, args.admin)
    else:
        logger.info(f"Starting clash service")
        start_clash(clash_core, config_path, clash_config, log_file, args.admin)


if __name__ == "__main__":
    main()
