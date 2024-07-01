import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from gemlock_parser.gemfile_lock import Gem, GemfileLockParser
from packageurl import PackageURL

from cachi2.core.errors import FetchError, UnsupportedFeature
from cachi2.core.models.input import Request, RubygemsPackageInput
from cachi2.core.models.output import Component, RequestOutput
from cachi2.core.package_managers.general import download_binary_file, extract_git_info
from cachi2.core.rooted_path import RootedPath
from cachi2.core.scm import clone_as_tarball

GEMFILE_LOCK = "Gemfile.lock"

GIT_REF_FORMAT = re.compile(r"^[a-fA-F0-9]{40}$")
PLATFORMS_RUBY = re.compile(r"^PLATFORMS\n {2}ruby\n\n", re.MULTILINE)

log = logging.getLogger(__name__)


def fetch_rubygems_source(request: Request) -> RequestOutput:
    components = []

    log.info("Resolving ruby dependencies")

    output_dir = request.output_dir.join_within_root("deps", "rubygems")
    output_dir.path.mkdir(parents=True, exist_ok=True)

    for package in request.rubygems_packages:
        info = _resolve_rubygems(request.source_dir, output_dir, package)
        components.append(Component.from_package_dict(info["package"]))
        for dependency in info["dependencies"]:
            components.append(
                Component(
                    name=dependency["name"],
                    version=dependency["version"],
                    purl="",
                )
            )

    return RequestOutput.from_obj_list(
        components,
        environment_variables=[],
        project_files=[],
    )


def _resolve_rubygems(
    source_dir: RootedPath,
    output_dir: RootedPath,
    package: RubygemsPackageInput,
):
    main_package_name, main_package_version = _get_metadata()
    purl = PackageURL(
        type="rubygems",
        name=main_package_name,
        version=main_package_version,
    )

    package_root = source_dir.join_within_root(package.path)
    gemlock_path = package_root.join_within_root(GEMFILE_LOCK)

    gems = _parse_gemlock(package_root, gemlock_path)
    dependencies = _download_dependencies(output_dir, gems, package_root, set())

    # for dep in dependencies:
    #     if dep["kind"] == "GIT":
    #         _prepare_git_dependency(dep["path"])

    return {
        "package": {
            "name": main_package_name,
            "version": main_package_version,
            "type": "rubygems",
            "path": package_root,
            "purl": purl.to_string(),
        },
        "dependencies": dependencies,
    }


def _get_metadata():
    return "name", "version"


@dataclass
class GemMetadata:
    name: str
    version: str
    type: str
    source: str
    branch: Optional[str] = None


def _parse_gemlock(
    source_dir: RootedPath,
    gemlock_path: RootedPath,
) -> list[GemMetadata]:
    _validate_gemlock_platforms(gemlock_path)

    dependencies = []
    all_gems: dict[str, Gem] = GemfileLockParser(str(gemlock_path)).all_gems
    for gem in all_gems.values():
        if gem.version is None:
            log.debug(
                f"Skipping RubyGem {gem.name}, because of a missing version. "
                f"This means gem is not used in a platform for which Gemfile.lock was generated."
            )
            continue

        _validate_gem_metadata(gem, source_dir, gemlock_path)
        source = gem.remote if gem.type != "PATH" else gem.path
        dependencies.append(
            GemMetadata(gem.name, gem.version, gem.type, source, gem.branch)
        )

    return dependencies


def _validate_gemlock_platforms(gemlock_path):
    with open(gemlock_path) as f:
        contents = f.read()

    if not PLATFORMS_RUBY.search(contents):
        msg = "PLATFORMS section of Gemfile.lock has to contain one and only platform - ruby."
        raise FetchError(msg)


def _validate_gem_metadata(gem, source_dir, gemlock_dir):
    if gem.type == "GEM":
        if gem.remote != "https://rubygems.org/":
            raise "Cachito supports only https://rubygems.org/ as a remote for Ruby GEM dependencies."

    elif gem.type == "GIT":
        if not gem.remote.startswith("https://"):
            raise "All Ruby GIT dependencies have to use HTTPS protocol."
        if not GIT_REF_FORMAT.match(gem.version):
            msg = (
                f"No git ref for gem: {gem.name} (expected 40 hexadecimal characters, "
                f"got: {gem.version})."
            )
            raise msg

    elif gem.type == "PATH":
        pass
        # _validate_path_dependency_dir(gem, source_dir, gemlock_dir)

    else:
        raise "Gemfile.lock contains unsupported dependency type."


def _validate_path_dependency_dir(gem, project_root, gemlock_dir):
    dependency_dir = gemlock_dir / Path(gem.path)
    try:
        dependency_dir = dependency_dir.resolve(strict=True)
        dependency_dir.relative_to(project_root.resolve())
    except FileNotFoundError:
        raise (
            f"PATH dependency {str(gem.name)} references a non-existing path: "
            f"{str(dependency_dir)}."
        )
    except RuntimeError:
        raise (
            f"Path of PATH dependency {str(gem.name)} contains an infinite loop: "
            f"{str(dependency_dir)}."
        )
    except ValueError:
        raise f"{str(dependency_dir)} is not a subpath of {str(project_root)}"


def _download_dependencies(
    output_dir: RootedPath,
    dependencies: list[GemMetadata],
    package_root: RootedPath,
    allowed_path_deps: set[str],
):
    downloads = []

    for dep in dependencies:
        log.info("Downloading %s (%s)", dep.name, dep.version)

        if dep.type == "GEM":
            download_info = _download_rubygems_package(dep, output_dir)
        elif dep.type == "GIT":
            download_info = _download_git_package(dep, output_dir)
        elif dep.type == "PATH":
            # _verify_path_dep_is_allowed(dep, allowed_path_deps)
            download_info = _get_path_package_info(dep, package_root)
        else:
            # Should not happen
            raise RuntimeError(f"Unexpected dependency type: {dep.type!r}")

        if dep.type != "PATH":
            log.info(
                "Successfully downloaded gem %s (%s) to %s",
                dep.name,
                dep.version,
                download_info["path"],
            )

        download_info["kind"] = dep.type
        download_info["type"] = "rubygems"
        downloads.append(download_info)

    return downloads


def _verify_path_dep_is_allowed(dep: GemMetadata, allowed_path_deps: set[str]):
    """
    Verify that PATH dependency is part of allowed_path_deps set.

    :param GemMetadata dep: dependency to check
    :param set[str] allowed_path_deps: allowed RubyGems PATH dependencies
    :raises UnsupportedFeature: when any not allowed PATH dependency should be downloaded
    """
    if dep.name not in allowed_path_deps:
        log.debug(f"rubygems_file_deps_allowlist: {allowed_path_deps}")
        raise UnsupportedFeature(
            f"PATH dependency {dep.name} is not allowed. "
            f"Please contact maintainers of this Cachito instance to allow it."
        )


def _download_rubygems_package(gem: GemMetadata, deps_dir: RootedPath):
    """Download platform independent RubyGem.

    The platform independence is ensured by downloading it from platform independent url
    (url that doesn't have any platform suffix).

    :param GemMetadata gem: Gem dependency from a Gemfile.lock file
    :param Path deps_dir: The deps/rubygems directory in a Cachito request bundle
    :param str proxy_url: URL of Nexus RubyGems proxy
    :param requests.auth.AuthBase proxy_auth: Authorization for the RubyGems proxy
    """
    download_path = deps_dir.join_within_root(f"{gem.name}-{gem.version}.gem")

    url = f"https://rubygems.org/gems/{gem.name}-{gem.version}.gem"
    download_binary_file(url, download_path)

    return {
        "name": gem.name,
        "version": gem.version,
        "path": download_path,
    }


def _download_git_package(gem: GemMetadata, deps_dir: RootedPath):
    """
    Fetch the source for a Ruby package from Git.

    If the package is already present in Nexus as a raw component, download it
    from there instead of fetching from the original location.

    :param GemMetadata gem: Git dependency from a Gemfile.lock file
    :param Path rubygems_deps_dir: The deps/rubygems directory in a Cachito request bundle
    :param str rubygems_raw_repo_name: Name of the Nexus raw repository for RubyGems
    :param requests.auth.AuthBase nexus_auth: Authorization for the Nexus raw repo

    :return: Dict with package name, download path, git url and ref, name of raw component in Nexus
        and boolean whether we already have the raw component in Nexus
    """
    git_info = extract_git_info(f"{gem.source}@{gem.version}")

    package_dir = deps_dir.join_within_root(
        git_info["host"],
        git_info["namespace"],
        git_info["repo"],
    )
    package_dir.path.mkdir(parents=True, exist_ok=True)

    clone_as_tarball(
        git_info["url"],
        git_info["ref"],
        to_path=package_dir.join_within_root("source.tar.gz"),
    )

    return {
        "name": gem.name,
        "version": gem.version,
        "path": package_dir,
        **git_info,
    }


def _get_path_package_info(dep: GemMetadata, package_root: RootedPath):
    path = package_root.join_within_root(dep.source).subpath_from_root

    return {
        "name": dep.name,
        "version": "./" + str(path),
    }


def _prepare_git_dependency(dep: GemMetadata):
    """
    Unpack the archive with the downloaded dependency and checkout a specified Git branch.

    Only the unpacked directory is kept, the archive is deleted.
    To get more info on local Git repos, see:
    https://bundler.io/man/bundle-config.1.html#LOCAL-GIT-REPOS
    :param dep: RubyGems GIT dependency
    """
    extracted_path = Path(str(dep["path"]).removesuffix(".tar.gz"))
    log.debug(f"Extracting archive at {dep['path']} to {extracted_path}")
    shutil.unpack_archive(dep["path"], extracted_path)
    os.remove(dep["path"])
    dep["path"] = extracted_path
