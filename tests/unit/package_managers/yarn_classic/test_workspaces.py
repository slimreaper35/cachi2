from pathlib import Path
from unittest import mock

import pytest

from hermeto.core.errors import PathOutsideRoot
from hermeto.core.package_managers.yarn_classic.project import PackageJson
from hermeto.core.package_managers.yarn_classic.workspaces import (
    Workspace,
    _extract_workspaces_globs,
    _get_workspace_paths,
    extract_workspace_metadata,
)
from hermeto.core.rooted_path import RootedPath


@mock.patch("hermeto.core.package_managers.yarn_classic.workspaces._get_workspace_paths")
def test_packages_with_workspaces_outside_source_dir_are_rejected(
    mock_get_ws_paths: mock.Mock,
    rooted_tmp_path: RootedPath,
) -> None:
    package_json_path = rooted_tmp_path.join_within_root("package.json")
    package_json_path.path.write_text('{"workspaces": ["../../usr"]}')
    mock_get_ws_paths.return_value = [Path("/usr")]

    with pytest.raises(PathOutsideRoot):
        extract_workspace_metadata(rooted_tmp_path)


@mock.patch("hermeto.core.package_managers.yarn_classic.workspaces._get_workspace_paths")
def test_workspaces_could_be_parsed(
    mock_get_ws_paths: mock.Mock,
    rooted_tmp_path: RootedPath,
) -> None:
    package_json_path = rooted_tmp_path.join_within_root("package.json")
    package_json_path.path.write_text('{"name": "outer_package", "workspaces": ["foo"]}')

    workspace_path = rooted_tmp_path.join_within_root("foo")
    workspace_path.path.mkdir()
    workspace_package_json_path = workspace_path.join_within_root("package.json")
    workspace_package_json_path.path.write_text('{"name": "inner_package"}')

    mock_get_ws_paths.return_value = [workspace_path.path]

    expected_result = [
        Workspace(
            path=workspace_path.path,
            package_json=PackageJson.from_file(workspace_package_json_path),
        ),
    ]
    result = extract_workspace_metadata(rooted_tmp_path)

    assert result == expected_result


@pytest.mark.parametrize(
    "package, expected",
    [
        pytest.param(
            {"workspaces": ["foo"]},
            ["foo"],
            id="workspaces_defined_in_an_array",
        ),
        pytest.param(
            {"workspaces": {"packages": ["foo"]}},
            ["foo"],
            id="workspaces_defined_in_an_array_within_an_object",
        ),
    ],
)
def test_extracting_workspace_globs_works_for_all_types_of_workspaces(
    package: dict,
    expected: list,
) -> None:
    result = _extract_workspaces_globs(package)

    assert expected == result


def test_get_workspace_paths(rooted_tmp_path: RootedPath) -> None:
    workspaces = rooted_tmp_path.join_within_root("packages")
    workspaces.path.mkdir()

    some_workspace = workspaces.join_within_root("some_workspace")
    some_workspace.path.mkdir()
    some_workspace.join_within_root("package.json").path.write_text('{"name": "some_workspace"}')

    some_directory = workspaces.join_within_root("some_directory")
    some_directory.path.mkdir()

    result = _get_workspace_paths(workspaces_globs=["packages/*"], source_dir=rooted_tmp_path)
    # should not return directories without package.json
    assert result == [some_workspace.path]
