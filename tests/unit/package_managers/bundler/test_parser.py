import json
import subprocess
import textwrap
from unittest import mock

import pydantic
import pytest

from cachi2.core.errors import PackageManagerError, PackageRejected, UnexpectedFormat
from cachi2.core.package_managers.bundler.parser import (
    GEMFILE,
    GEMFILE_LOCK,
    GemDependency,
    GitDependency,
    PathDependency,
    parse_lockfile,
)
from cachi2.core.rooted_path import RootedPath


@pytest.fixture
def empty_gemfile(rooted_tmp_path: RootedPath) -> RootedPath:
    gemfile_path = rooted_tmp_path.join_within_root(GEMFILE)
    gemfile_path.path.touch()
    return gemfile_path


MOCKED_BASE_JSON = textwrap.dedent(
    """
    {
      "bundler_version": "2.5.10",
      "dependencies": [
        {
          "name": "example",
          "version": "0.1.0"
        }
      ]
    }
    """
)


def test_parse_lockfile_without_lockfile(rooted_tmp_path: RootedPath) -> None:
    msg = "Gemfile and Gemfile.lock must be present in the package directory"
    with pytest.raises(PackageRejected, match=msg):
        parse_lockfile(rooted_tmp_path)


@mock.patch("cachi2.core.package_managers.bundler.parser.run_cmd")
def test_parse_lockfile_os_error(
    mock_run_cmd: mock.MagicMock,
    empty_gemfile: RootedPath,
    rooted_tmp_path: RootedPath,
) -> None:
    lockfile_path = rooted_tmp_path.join_within_root(GEMFILE_LOCK)
    lockfile_path.path.touch()

    mock_run_cmd.side_effect = subprocess.CalledProcessError(returncode=1, cmd="cmd")

    with pytest.raises(PackageManagerError) as exc_info:
        parse_lockfile(rooted_tmp_path)

    assert f"Failed to parse {lockfile_path}" == str(exc_info.value)


LOCKFILE_INVALID_URL = textwrap.dedent(
    """
    GIT
      remote: github
      revision: 26487618a68443e94d623bb585cb464b07d36702
      specs:
        json-schema (1.0.0)

    PLATFORMS
      ruby

    DEPENDENCIES
      json-schema!

    BUNDLED WITH
      2.5.10
    """
)

LOCKFILE_INVALID_URL_SCHEME = textwrap.dedent(
    """
    GIT
      remote: http://github.com/3scale/json-schema.git
      revision: 26487618a68443e94d623bb585cb464b07d36702
      specs:
        json-schema (1.0.0)

    PLATFORMS
      ruby

    DEPENDENCIES
      json-schema!

    BUNDLED WITH
      2.5.10
    """
)

LOCKFILE_INVALID_REVISION = textwrap.dedent(
    """
    GIT
      remote: https://github.com/3scale/json-schema.git
      revision: abcd1234
      specs:
        json-schema (1.0.0)

    PLATFORMS
      ruby

    DEPENDENCIES
      json-schema!

    BUNDLED WITH
      2.5.10
    """
)

LOCKFILE_INVALID_PATH = textwrap.dedent(
    """
    PATH
      remote: /root/pathgem
      specs:
        pathgem (1.0.0)

    PLATFORMS
      ruby

    DEPENDENCIES
      pathgem!

    BUNDLED WITH
      2.5.10
    """
)


@mock.patch("cachi2.core.package_managers.bundler.parser.run_cmd")
@pytest.mark.parametrize(
    "lockfile_content, expected_error",
    [
        (LOCKFILE_INVALID_URL, "Input should be a valid URL"),
        (LOCKFILE_INVALID_URL_SCHEME, "URL scheme should be 'https'"),
        (LOCKFILE_INVALID_REVISION, "String should match pattern '^[a-fA-F0-9]{40}$'"),
        (LOCKFILE_INVALID_PATH, "PATH dependencies should be within the package root"),
    ],
)
def test_parse_lockfile_invalid_format(
    mock_run_cmd: mock.MagicMock,
    lockfile_content: str,
    expected_error: str,
    empty_gemfile: RootedPath,
    rooted_tmp_path: RootedPath,
) -> None:
    lockfile_path = rooted_tmp_path.join_within_root(GEMFILE_LOCK)
    lockfile_path.path.write_text(lockfile_content)

    mocked_json = json.loads(MOCKED_BASE_JSON)

    if lockfile_content == LOCKFILE_INVALID_URL:
        mocked_json["dependencies"][0].update(
            {
                "type": "git",
                "url": "github",
                "revision": "f" * 40,
            }
        )
    elif lockfile_content == LOCKFILE_INVALID_URL_SCHEME:
        mocked_json["dependencies"][0].update(
            {
                "type": "git",
                "url": "http://github.com/3scale/json-schema.git",
                "revision": "f" * 40,
            }
        )
    elif lockfile_content == LOCKFILE_INVALID_REVISION:
        mocked_json["dependencies"][0].update(
            {
                "type": "git",
                "url": "https://github.com/3scale/json-schema.git",
                "revision": "abcd",
            }
        )
    elif lockfile_content == LOCKFILE_INVALID_PATH:
        mocked_json["dependencies"][0].update(
            {
                "type": "path",
                "path": "/root/pathgem",
            }
        )

    mock_run_cmd.return_value = json.dumps(mocked_json)
    with pytest.raises((pydantic.ValidationError, UnexpectedFormat)) as exc_info:
        parse_lockfile(rooted_tmp_path)

    assert expected_error in str(exc_info.value)


LOCKFILE_VALID = textwrap.dedent(
    """
    GIT
      remote: https://github.com/3scale/json-schema.git
      revision: 26487618a68443e94d623bb585cb464b07d36702
      branch: master
      specs:
        json-schema (1.0.0)

    PATH
      remote: subpath/pathgem
      specs:
        pathgem (2.0.0)

    GEM
      remote: https://rubygems.org/
      specs:
        gem (3.0.0)

    PLATFORMS
      ruby
      x86_64-linux

    DEPENDENCIES
      json-schema!
      pathgem!
      gem

    BUNDLED WITH
      2.5.10
    """
)


@mock.patch("cachi2.core.package_managers.bundler.parser.run_cmd")
def test_parse_gemlock(
    mock_run_cmd: mock.MagicMock,
    empty_gemfile: RootedPath,
    rooted_tmp_path: RootedPath,
    caplog: pytest.LogCaptureFixture,
) -> None:
    lockfile_path = rooted_tmp_path.join_within_root(GEMFILE_LOCK)
    lockfile_path.path.write_text(LOCKFILE_VALID)

    mocked_json = json.loads(MOCKED_BASE_JSON)
    base_dep = mocked_json["dependencies"][0]

    mocked_json["dependencies"] = [
        {
            "type": "git",
            "url": "https://github.com/3scale/json-schema.git",
            "revision": "f" * 40,
            "branch": "master",
            **base_dep,
        },
        {
            "type": "path",
            "path": "subpath/pathgem",
            **base_dep,
        },
        {
            "type": "rubygems",
            "source": "https://rubygems.org/",
            **base_dep,
        },
    ]

    mock_run_cmd.return_value = json.dumps(mocked_json)
    result = parse_lockfile(rooted_tmp_path)

    expected_deps = [
        GitDependency(
            name="example",
            version="0.1.0",
            url="https://github.com/3scale/json-schema.git",
            revision="f" * 40,
            branch="master",
        ),
        PathDependency(name="example", version="0.1.0", path="subpath/pathgem"),
        GemDependency(name="example", version="0.1.0", source="https://rubygems.org/"),
    ]

    assert f"Package {rooted_tmp_path.path.name} is bundled with version 2.5.10" in caplog.messages
    assert result == expected_deps


LOCKFILE_VALID_EMPTY = textwrap.dedent(
    """
    GEM
      remote: https://rubygems.org/
      specs:

    PLATFORMS
      ruby
      x86_64-linux

    DEPENDENCIES

    BUNDLED WITH
      2.5.10
    """
)


@mock.patch("cachi2.core.package_managers.bundler.parser.run_cmd")
def test_parse_gemlock_empty(
    mock_run_cmd: mock.MagicMock,
    empty_gemfile: RootedPath,
    rooted_tmp_path: RootedPath,
    caplog: pytest.LogCaptureFixture,
) -> None:
    lockfile_path = rooted_tmp_path.join_within_root(GEMFILE_LOCK)
    lockfile_path.path.write_text(LOCKFILE_VALID_EMPTY)

    mock_run_cmd.return_value = '{"bundler_version": "2.5.10", "dependencies": []}'
    result = parse_lockfile(rooted_tmp_path)

    assert f"Package {rooted_tmp_path.path.name} is bundled with version 2.5.10" in caplog.messages
    assert result == []
