import os
from pathlib import Path

from cachi2.interface.cli import fetch_deps, generate_env, inject_files

HOME = Path(os.environ["HOME"])

SOURCE_DIR = HOME.joinpath("cachi2")
OUTPUT_DIR = SOURCE_DIR.joinpath("cachi2-output")


def debug_fetch_deps():
    package_manager = "pip"
    source_dir = SOURCE_DIR
    output_dir = OUTPUT_DIR.joinpath("cachi2-output")

    fetch_deps(package_manager, source_dir, output_dir)


def debug_generate_env():
    from_output_dir = OUTPUT_DIR
    # Dockerfile:
    # RUN . /tmp/cachi2.env
    for_output_dir = Path("/tmp")
    output = Path("cahi2.env")

    generate_env(from_output_dir, for_output_dir, output)


def debug_inject_files():
    from_output_dir = OUTPUT_DIR
    for_output_dir = Path("/tmp")

    inject_files(from_output_dir, for_output_dir)


def main():
    debug_fetch_deps()
    debug_generate_env()
    debug_inject_files()


if __name__ == "__main__":
    main()
