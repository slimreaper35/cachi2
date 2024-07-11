import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from gemlock_parser.gemfile_lock import Gem, GemfileLockParser  # type: ignore
from git import Repo
from packageurl import PackageURL

from cachi2.core.errors import FetchError
from cachi2.core.models.input import Request, RubygemsPackageInput
from cachi2.core.models.output import Component, EnvironmentVariable, ProjectFile, RequestOutput
from cachi2.core.package_managers.general import download_binary_file, extract_git_info
from cachi2.core.rooted_path import RootedPath
from cachi2.core.scm import clone_as_tarball, get_repo_id

GEMFILE_LOCK = "Gemfile.lock"

GIT_REF_FORMAT = re.compile(r"^[a-fA-F0-9]{40}$")
PLATFORMS_RUBY = re.compile(r"^PLATFORMS\n {2}ruby\n\n", re.MULTILINE)

log = logging.getLogger(__name__)


def fetch_rubygems_source(request: Request) -> RequestOutput:
    """Resolve and fetch RubyGems dependencies."""
    components = []
    environment_variables = [
        EnvironmentVariable(name="BUNDLE_APP_CONFIG", value="${output_dir}/.bundle/config"),
    ]
    project_files: list[ProjectFile] = []

    output_dir = request.output_dir.join_within_root("deps", "rubygems")
    output_dir.path.mkdir(parents=True, exist_ok=True)

    bundle_config = request.output_dir.join_within_root(".bundle", "config")
    bundle_config.path.parent.mkdir(parents=True, exist_ok=True)

    for package in request.rubygems_packages:
        info = _resolve_rubygems(request.source_dir, output_dir, package)
        components.append(Component.from_package_dict(info["package"]))
        for dependency in info["dependencies"]:
            components.append(
                Component(
                    name=dependency["name"],
                    version=dependency["version"],
                    purl=dependency["purl"],
                )
            )

    return RequestOutput.from_obj_list(
        components,
        environment_variables=environment_variables,
        project_files=project_files,
    )


def _resolve_rubygems(
    source_dir: RootedPath,
    output_dir: RootedPath,
    package: RubygemsPackageInput,
) -> dict[str, Any]:
    package_root = source_dir.join_within_root(package.path)
    gemlock_path = package_root.join_within_root(GEMFILE_LOCK)

    gems = _parse_gemlock(package_root, gemlock_path)

    main_package_name, main_package_version = _get_package_metadata(source_dir, gems)
    purl = PackageURL(
        type="rubygems",
        name=main_package_name,
        version=main_package_version,
    )

    dependencies = _download_dependencies(output_dir, gems, package_root, set())

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


def _get_package_metadata(package_dir: RootedPath, dependencies) -> tuple[str, Optional[str]]:
    """TODO."""
    for dep in dependencies:
        if dep.path == ".":
            log.info("Found main package %s (%s)", dep.name, dep.version)
            return dep.name, dep.version

    repo_path = get_repo_id(package_dir.root).parsed_origin_url.path.removesuffix(".git")
    repo_name = Path(repo_path).name
    package_subpath = package_dir.subpath_from_root

    resolved_path = Path(repo_name).joinpath(package_subpath)
    name = resolved_path.name

    log.info("Found main package %s", name)
    return name, None


@dataclass
class GemMetadata:
    """Gem metadata."""

    name: str
    version: str
    type: str
    source: str
    branch: Optional[str] = None
    path: Optional[str] = None


def _parse_gemlock(
    source_dir: RootedPath,
    gemlock_path: RootedPath,
) -> list[GemMetadata]:
    _validate_gemlock_platforms(gemlock_path)

    dependencies = []
    parser = GemfileLockParser(str(gemlock_path))
    log.info("Bundled with version %s", parser.bundled_with)

    for gem in parser.all_gems.values():
        if gem.version is None:
            log.debug(
                f"Skipping RubyGem {gem.name}, because of a missing version. "
                f"This means gem is not used in a platform for which Gemfile.lock was generated."
            )
            continue

        _validate_gem_metadata(gem, source_dir, gemlock_path.root)
        source = gem.remote if gem.type != "PATH" else gem.path
        dependencies.append(
            GemMetadata(gem.name, gem.version, gem.type, source, gem.branch, gem.path)
        )

    return dependencies


def _validate_gemlock_platforms(gemlock_path: RootedPath) -> None:
    with open(gemlock_path) as f:
        contents = f.read()

    if not PLATFORMS_RUBY.search(contents):
        msg = (
            "Platforms other than 'ruby' were found in the Gemfile.lock. "
            "Notice that Cachi2 will only download Gems from the 'ruby' platform. "
            "All other platform-specific Gems will be skipped."
        )
        log.warning(msg)


def _validate_gem_metadata(gem: Gem, source_dir: RootedPath, gemlock_dir: Path) -> None:
    if gem.type == "GEM":
        if gem.remote != "https://rubygems.org/":
            raise Exception(
                "Cachito supports only https://rubygems.org/ as a remote for Ruby GEM dependencies."
            )

    elif gem.type == "GIT":
        if not gem.remote.startswith("https://"):
            raise Exception("All Ruby GIT dependencies have to use HTTPS protocol.")
        if not GIT_REF_FORMAT.match(gem.version):
            msg = (
                f"No git ref for gem: {gem.name} (expected 40 hexadecimal characters, "
                f"got: {gem.version})."
            )
            raise Exception(msg)

    elif gem.type == "PATH":
        _validate_path_dependency_dir(gem, source_dir, gemlock_dir)

    else:
        raise Exception("Gemfile.lock contains unsupported dependency type.")


def _validate_path_dependency_dir(gem: Gem, source_dir: RootedPath, gemlock_dir: Path) -> None:
    dependency_dir = gemlock_dir.joinpath(gem.path)
    try:
        dependency_dir = dependency_dir.resolve(strict=True)
        dependency_dir.relative_to(source_dir)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"PATH dependency {str(gem.name)} references a non-existing path: "
            f"{str(dependency_dir)}."
        )
    except RuntimeError:
        raise RuntimeError(
            f"Path of PATH dependency {str(gem.name)} contains an infinite loop: "
            f"{str(dependency_dir)}."
        )
    except ValueError:
        raise ValueError(f"{str(dependency_dir)} is not a subpath of {str(source_dir)}")


def _download_dependencies(
    output_dir: RootedPath,
    dependencies: list[GemMetadata],
    package_root: RootedPath,
    allowed_path_deps: set[str],
) -> list[dict[str, Any]]:
    downloads = []

    for dep in dependencies:
        log.info("Downloading %s (%s)", dep.name, dep.version)

        if dep.type == "GEM":
            download_info = _download_rubygems_package(dep, output_dir)
        elif dep.type == "GIT":
            download_info = _download_git_package(dep, output_dir)
        elif dep.type == "PATH":
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
        download_info["purl"] = PackageURL(
            type="rubygems",
            name=dep.name,
            version=dep.version,
        ).to_string()
        downloads.append(download_info)

    return downloads


def _download_rubygems_package(gem: GemMetadata, deps_dir: RootedPath) -> dict[str, Any]:
    download_path = deps_dir.join_within_root(f"{gem.name}-{gem.version}.gem")

    url = f"https://rubygems.org/gems/{gem.name}-{gem.version}.gem"
    download_binary_file(url, download_path.path)

    return {
        "name": gem.name,
        "version": gem.version,
        "path": download_path,
    }


def _download_git_package(gem: GemMetadata, deps_dir: RootedPath) -> dict[str, Any]:
    git_info = extract_git_info(f"{gem.source}@{gem.version}")

    name = git_info["repo"]
    short_ref = git_info["ref"][:12]

    git_package_path = deps_dir.join_within_root(f"{name}-{short_ref}")

    if not git_package_path.path.exists():
        git_package_path.path.mkdir(parents=True, exist_ok=True)

        Repo.clone_from(
            url=git_info["url"],
            to_path=git_package_path.path,
            branch=gem.branch,
        )

    return {
        "name": gem.name,
        "version": gem.version,
        "path": git_package_path,
    }


def _get_path_package_info(dep: GemMetadata, package_root: RootedPath) -> dict[str, Any]:
    path = package_root.join_within_root(dep.source).subpath_from_root

    return {
        "name": dep.name,
        "version": dep.version,
        "path": path,
    }
