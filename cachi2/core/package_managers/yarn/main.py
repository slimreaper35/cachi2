import logging

import semver

from cachi2.core.errors import PackageManagerError, PackageRejected
from cachi2.core.models.input import Request
from cachi2.core.models.output import Component, EnvironmentVariable, RequestOutput
from cachi2.core.package_managers.yarn.project import (
    Plugin,
    Project,
    YarnRc,
    get_semver_from_package_manager,
    get_semver_from_yarn_path,
)
from cachi2.core.package_managers.yarn.resolver import create_components, resolve_packages
from cachi2.core.package_managers.yarn.utils import (
    VersionsRange,
    extract_yarn_version_from_env,
    run_yarn_cmd,
)
from cachi2.core.rooted_path import RootedPath

log = logging.getLogger(__name__)


def fetch_yarn_source(request: Request) -> RequestOutput:
    """Process all the yarn source directories in a request."""
    components = []

    for package in request.yarn_packages:
        path = request.source_dir.join_within_root(package.path)
        project = Project.from_source_dir(path)

        components.extend(_resolve_yarn_project(project, request.output_dir))

    return RequestOutput.from_obj_list(
        components, _generate_environment_variables(), project_files=[]
    )


def _verify_yarnrc_paths(project: Project) -> None:
    paths_conf_opts = {
        # pnpDataPath is only configurable in Yarn v3
        project.yarn_rc.pnp_data_path: "pnpDataPath",
        project.yarn_rc.pnp_unplugged_folder: "pnpUnpluggedFolder",
        project.yarn_rc.install_state_path: "installStatePath",
        project.yarn_rc.patch_folder: "patchFolder",
        project.yarn_rc.virtual_folder: "virtualFolder",
    }

    for path in paths_conf_opts:
        if path is not None:
            try:
                project.source_dir.join_within_root(path)
            except Exception:
                raise PackageRejected(
                    (
                        f"YarnRC '{paths_conf_opts[path]}={path}' property: path points "
                        "outside of the source directory"
                    ),
                    solution=(
                        "Make sure that all Yarn RC configuration options specifying a path "
                        "point to a relative location inside the main repository"
                    ),
                )


def _check_zero_installs(project: Project) -> None:
    if project.is_zero_installs:
        raise PackageRejected(
            ("Yarn zero install detected, PnP zero installs are unsupported by cachi2"),
            solution=(
                "Please convert your project to a regular install-based one.\n"
                "Depending on whether you use Yarn's PnP or a different node linker Yarn setting "
                "make sure to remove '.yarn/cache' or 'node_modules' directories respectively."
            ),
        )


def _check_lockfile(project: Project) -> None:
    if not project.source_dir.join_within_root(project.yarn_rc.lockfilename).path.exists():
        raise PackageRejected(
            f"Yarn lockfile '{project.yarn_rc.lockfilename}' missing, refusing to continue",
            solution=(
                "Make sure your repository has a Yarn lockfile (e.g. yarn.lock) checked in "
                "to the repository"
            ),
        )


def _verify_repository(project: Project) -> None:
    _verify_yarnrc_paths(project)
    _check_zero_installs(project)
    _check_lockfile(project)


def _resolve_yarn_project(project: Project, output_dir: RootedPath) -> list[Component]:
    """Process a request for a single yarn source directory.

    :param project: the directory to be processed.
    :param output_dir: the directory where the prefetched dependencies will be placed.
    :raises PackageManagerError: if fetching dependencies fails
    """
    log.info(f"Fetching the yarn dependencies at the subpath {project.source_dir}")

    _configure_yarn_version(project)
    _verify_repository(project)

    _set_yarnrc_configuration(project, output_dir)
    packages = resolve_packages(project.source_dir)
    _fetch_dependencies(project.source_dir)

    return create_components(packages, project, output_dir)


def _configure_yarn_version(project: Project) -> None:
    """Resolve the yarn version and set it in the package.json file if needed.

    :raises PackageRejected:
        if the yarn version can't be determined from either yarnPath or packageManager
        if there is a mismatch between the yarn version specified by yarnPath and PackageManager
    """
    yarn_path_version = get_semver_from_yarn_path(project.yarn_rc.yarn_path)
    package_manager_version = get_semver_from_package_manager(project.package_json.package_manager)

    if yarn_path_version is None and package_manager_version is None:
        raise PackageRejected(
            "Unable to determine the yarn version to use to process the request",
            solution=(
                "Ensure that either yarnPath is defined in .yarnrc.yml or that packageManager "
                "is defined in package.json"
            ),
        )

    version = yarn_path_version if yarn_path_version else package_manager_version
    # By this point version is not Optional anymore, but mypy does not think so.
    if version not in VersionsRange("3.0.0", "5.0.0"):  # type: ignore
        raise PackageRejected(
            f"Unsupported Yarn version '{version}' detected",
            solution="Please pick a different version of Yarn (3.0.0<= Yarn version <5.0.0)",
        )

    if (
        yarn_path_version
        and package_manager_version
        and yarn_path_version != package_manager_version
    ):
        raise PackageRejected(
            (
                f"Mismatch between the yarn versions specified by yarnPath (yarn@{yarn_path_version}) "
                f"and packageManager (yarn@{package_manager_version})"
            ),
            solution=(
                "Ensure that the versions of yarn specified by yarnPath in .yarnrc.yml and "
                "packageManager in package.json agree"
            ),
        )

    if not package_manager_version:
        project.package_json.package_manager = f"yarn@{yarn_path_version}"
        project.package_json.write()

    # Note (mypy): version cannot be None anymore
    _verify_corepack_yarn_version(version, project.source_dir)  # type: ignore


def _get_plugin_allowlist(yarn_rc: YarnRc) -> list[Plugin]:
    """Return a list of plugins that can be kept in .yarnrc.yml.

    Some plugins are required for processing a specific protocol (e.g. exec), and their absence
    would make yarn commands such as 'install' and 'info' fail. Keeping this whitelist allows
    Cachi2 to get the list of packages from 'yarn info' and properly inform the user if his request
    is not processable in case it contains disallowed protocols.

    This list should only have official plugins that add new protocols and that also do not
    implement the 'fetchPackageInfo' hook, since it would allow arbitrary code execution.

    Note that starting from v4, the official plugins are enabled by default and can't be disabled.
    Since they're not present in the .yarnrc.yml file anymore, this function has no effect on v4
    projects.

    See https://v3.yarnpkg.com/advanced/plugin-tutorial#hook-fetchPackageInfo.
    """
    default_plugins = [
        Plugin(path=".yarn/plugins/@yarnpkg/plugin-exec.cjs", spec="@yarnpkg/plugin-exec"),
    ]

    return [plugin for plugin in default_plugins if plugin in yarn_rc.plugins]


def _set_yarnrc_configuration(project: Project, output_dir: RootedPath) -> None:
    """Set all the necessary configuration in yarnrc for the project processing.

    :param project: a Project instance
    :param output_dir: in case the dependencies need to be fetched, this is where they will be
        downloaded to.
    """
    yarn_rc = project.yarn_rc

    yarn_rc.plugins = _get_plugin_allowlist(yarn_rc)
    yarn_rc.checksum_behavior = "throw"
    yarn_rc.enable_immutable_installs = True
    yarn_rc.pnp_mode = "strict"
    yarn_rc.enable_strict_ssl = True
    yarn_rc.enable_telemetry = False
    yarn_rc.ignore_path = True
    yarn_rc.unsafe_http_whitelist = []
    yarn_rc.enable_mirror = False
    yarn_rc.enable_scripts = False
    yarn_rc.enable_global_cache = True
    yarn_rc.global_folder = str(output_dir.join_within_root("deps", "yarn"))

    # version can be read from `package.json` since we have already executed
    # `_configure_yarn_version` at this point
    version = get_semver_from_package_manager(project.package_json.package_manager)

    # In Yarn v4, constraints can be automatically executed as part of `yarn install`, so they
    # need to be explicitly disabled
    if version in VersionsRange("4.0.0-rc1", "5.0.0"):  # type: ignore
        yarn_rc.enable_constraints_checks = False

    yarn_rc.write()


def _fetch_dependencies(source_dir: RootedPath) -> None:
    """Fetch dependencies using 'yarn install'.

    :param source_dir: the directory in which the yarn command will be called.
    :raises PackageManagerError: if the 'yarn install' command fails.
    """
    run_yarn_cmd(["install", "--mode", "skip-build"], source_dir)


def _generate_environment_variables() -> list[EnvironmentVariable]:
    """Generate environment variables that will be used for building the project."""
    env_vars = {
        "YARN_ENABLE_GLOBAL_CACHE": "false",
        "YARN_ENABLE_IMMUTABLE_CACHE": "false",
        "YARN_ENABLE_MIRROR": "true",
        "YARN_GLOBAL_FOLDER": "${output_dir}/deps/yarn",
    }

    return [EnvironmentVariable(name=key, value=value) for key, value in env_vars.items()]


def _verify_corepack_yarn_version(expected_version: semver.Version, source_dir: RootedPath) -> None:
    """Verify that corepack installed the correct version of yarn by checking `yarn --version`."""
    installed_yarn_version = extract_yarn_version_from_env(source_dir)
    if installed_yarn_version != expected_version:
        raise PackageManagerError(
            f"Cachi2 expected corepack to install yarn@{expected_version} but instead "
            f"found yarn@{installed_yarn_version}."
        )

    log.info("Processing the request using yarn@%s", installed_yarn_version)
