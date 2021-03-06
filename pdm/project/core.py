from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Type, Union

import halo
import tomlkit
from pip._vendor.pkg_resources import safe_name
from pip_shims import shims
from vistir.contextmanagers import atomic_open_for_write

from pdm._types import Source
from pdm.exceptions import ProjectError
from pdm.iostream import stream
from pdm.models.caches import CandidateInfoCache, HashCache
from pdm.models.candidates import Candidate, identify
from pdm.models.environment import Environment, GlobalEnvironment
from pdm.models.repositories import BaseRepository, PyPIRepository
from pdm.models.requirements import Requirement, parse_requirement, strip_extras
from pdm.models.specifiers import PySpecSet
from pdm.project.config import Config
from pdm.project.meta import PackageMeta
from pdm.resolver import BaseProvider, EagerUpdateProvider, ReusePinProvider
from pdm.resolver.reporters import SpinnerReporter
from pdm.utils import cached_property, find_project_root, get_venv_python

if TYPE_CHECKING:
    from tomlkit.container import Container


class Project:
    """Core project class"""

    PYPROJECT_FILENAME = "pyproject.toml"
    PDM_NAMESPACE = "tool.pdm"
    DEPENDENCIES_RE = re.compile(r"(?:(.+?)-)?dependencies")
    PYPROJECT_VERSION = "0.0.1"
    GLOBAL_PROJECT = Path.home() / ".pdm" / "global-project"

    @classmethod
    def create_global(cls, root_path: Optional[str] = None) -> "Project":
        if root_path is None:
            root_path = cls.GLOBAL_PROJECT.as_posix()
        project = cls(root_path)
        project.is_global = True
        project.init_global_project()
        return project

    def __init__(self, root_path: Optional[str] = None) -> None:
        self.is_global = False
        self._pyproject = None  # type: Optional[Container]
        self._lockfile = None  # type: Optional[Container]
        self.core = None

        if root_path is None:
            root_path = find_project_root()
        if root_path is None and self.global_config["auto_global"]:
            self.root = self.GLOBAL_PROJECT
            self.is_global = True
            self.init_global_project()
        else:
            self.root = Path(root_path or "").absolute()

    def __repr__(self) -> str:
        return f"<Project '{self.root.as_posix()}'>"

    @property
    def pyproject_file(self) -> Path:
        return self.root / self.PYPROJECT_FILENAME

    @property
    def lockfile_file(self) -> Path:
        return self.root / "pdm.lock"

    @property
    def pyproject(self):
        # type: () -> Container
        if not self._pyproject and self.pyproject_file.exists():
            data = tomlkit.parse(self.pyproject_file.read_text("utf-8"))
            self._pyproject = data
        return self._pyproject

    @pyproject.setter
    def pyproject(self, data):
        self._pyproject = data

    @property
    def tool_settings(self):
        # type: () -> Union[Container, Dict]
        data = self.pyproject
        if not data:
            return {}
        for sec in self.PDM_NAMESPACE.split("."):
            # setdefault has bug
            if sec not in data:
                data[sec] = {}
            data = data[sec]
        return data

    @property
    def lockfile(self):
        # type: () -> Container
        if not self.lockfile_file.is_file():
            raise ProjectError("Lock file does not exist.")
        if not self._lockfile:
            data = tomlkit.parse(self.lockfile_file.read_text("utf-8"))
            self._lockfile = data
        return self._lockfile

    @property
    def config(self) -> Dict[str, Any]:
        """A read-only dict configuration, any modifications won't land in the file."""
        result = dict(self.global_config)
        result.update(self.project_config)
        return result

    @cached_property
    def global_config(self) -> Config:
        """Read-and-writable configuration dict for global settings"""
        return Config(Path.home() / ".pdm" / "config.toml", is_global=True)

    @cached_property
    def project_config(self) -> Config:
        """Read-and-writable configuration dict for project settings"""
        return Config(self.root / ".pdm.toml")

    @property
    def is_pdm(self) -> bool:
        if not self.pyproject_file.is_file():
            return False
        return bool(self.tool_settings)

    @cached_property
    def environment(self) -> Environment:
        if self.is_global:
            return GlobalEnvironment(self)
        if self.config["use_venv"] and "VIRTUAL_ENV" in os.environ:
            self.project_config["python.path"] = get_venv_python()
            return GlobalEnvironment(self)
        return Environment(self)

    @property
    def python_requires(self) -> PySpecSet:
        return PySpecSet(self.tool_settings.get("python_requires", ""))

    def get_dependencies(self, section: Optional[str] = None) -> Dict[str, Requirement]:
        if section in (None, "default"):
            deps = self.tool_settings.get("dependencies", [])
        elif section == "dev":
            deps = self.tool_settings.get("dev-dependencies", [])
        else:
            deps = self.tool_settings[f"{section}-dependencies"]
        result = {}
        for name, dep in deps.items():
            req = Requirement.from_req_dict(name, dep)
            req.from_section = section or "default"
            result[identify(req)] = req
        return result

    @property
    def dependencies(self) -> Dict[str, Requirement]:
        return self.get_dependencies()

    @property
    def dev_dependencies(self) -> Dict[str, Requirement]:
        return self.get_dependencies("dev")

    def iter_sections(self) -> Iterable[str]:
        for key in self.tool_settings:
            match = self.DEPENDENCIES_RE.match(key)
            if not match:
                continue
            section = match.group(1) or "default"
            yield section

    @property
    def all_dependencies(self) -> Dict[str, Dict[str, Requirement]]:
        return {
            section: self.get_dependencies(section) for section in self.iter_sections()
        }

    @property
    def allow_prereleases(self) -> Optional[bool]:
        return self.tool_settings.get("allow_prereleases")

    @property
    def sources(self) -> List[Source]:
        sources = self.tool_settings.get("source", [])
        if not any(source.get("name") == "pypi" for source in sources):
            sources.insert(
                0,
                {
                    "url": self.config["pypi.url"],
                    "verify_ssl": self.config["pypi.verify_ssl"],
                    "name": "pypi",
                },
            )
        return sources

    def get_repository(self, cls: Optional[Type[BaseRepository]]) -> BaseRepository:
        """Get the repository object"""
        if cls is None:
            cls = PyPIRepository
        sources = self.sources or []
        return cls(sources, self.environment)

    def get_provider(
        self, strategy: str = "all", tracked_names: Optional[Iterable[str]] = None,
    ) -> BaseProvider:
        """Build a provider class for resolver.

        :param strategy: the resolve strategy
        :param tracked_names: the names of packages that needs to update
        :returns: The provider object
        """
        repository = self.get_repository(cls=self.core.repository_class)
        allow_prereleases = self.allow_prereleases
        requires_python = self.environment.python_requires
        if strategy == "all":
            provider = BaseProvider(repository, requires_python, allow_prereleases)
        else:
            provider_class = (
                ReusePinProvider if strategy == "reuse" else EagerUpdateProvider
            )
            preferred_pins = self.get_locked_candidates("__all__")
            provider = provider_class(
                preferred_pins,
                tracked_names or (),
                repository,
                requires_python,
                allow_prereleases,
            )
        return provider

    def get_reporter(
        self,
        requirements: Dict[str, Dict[str, Requirement]],
        tracked_names: Optional[Iterable[str]] = None,
        spinner: Optional[halo.Halo] = None,
    ) -> SpinnerReporter:
        """Return the reporter object to construct a resolver.

        :param requirements: requirements to resolve
        :param tracked_names: the names of packages that needs to update
        :param spinner: optional spinner object
        :returns: a reporter
        """
        return SpinnerReporter(spinner)

    def get_project_metadata(self) -> Dict[str, Any]:
        content_hash = self.get_content_hash("md5")
        data = {
            "meta_version": self.PYPROJECT_VERSION,
            "content_hash": f"md5:{content_hash}",
        }
        return data

    def write_lockfile(self, toml_data: Container, show_message: bool = True) -> None:
        toml_data.update({"root": self.get_project_metadata()})

        with atomic_open_for_write(self.lockfile_file) as fp:
            fp.write(tomlkit.dumps(toml_data))
        if show_message:
            stream.echo("Changes are written to pdm.lock.")
        self._lockfile = None

    def make_self_candidate(self, editable: bool = True) -> Candidate:
        req = parse_requirement(self.root.as_posix(), editable)
        req.name = self.meta.name
        return Candidate(
            req, self.environment, name=self.meta.name, version=self.meta.version
        )

    def get_locked_candidates(
        self, section: Optional[str] = None
    ) -> Dict[str, Candidate]:
        if not self.lockfile_file.is_file():
            return {}
        section = section or "default"
        result = {}
        for package in [dict(p) for p in self.lockfile.get("package", [])]:
            if section != "__all__" and section not in package["sections"]:
                continue
            version = package.get("version")
            if version:
                package["version"] = f"=={version}"
            package_name = package.pop("name")
            req = Requirement.from_req_dict(package_name, dict(package))
            can = Candidate(req, self.environment, name=package_name, version=version)
            can.marker = req.marker
            can.hashes = {
                item["file"]: item["hash"]
                for item in self.lockfile["metadata"].get(
                    f"{package_name} {version}", []
                )
            } or None
            result[identify(req)] = can
        if section in ("default", "__all__") and self.meta.name and self.meta.version:
            result[safe_name(self.meta.name).lower()] = self.make_self_candidate(True)
        return result

    def get_content_hash(self, algo: str = "md5") -> str:
        # Only calculate sources and dependencies sections. Otherwise lock file is
        # considered as unchanged.
        dump_data = {"sources": self.tool_settings.get("source", [])}
        for section in self.iter_sections():
            toml_section = (
                "dependencies" if section == "default" else f"{section}-dependencies"
            )
            dump_data[toml_section] = dict(self.tool_settings.get(toml_section, {}))
        pyproject_content = json.dumps(dump_data, sort_keys=True)
        hasher = hashlib.new(algo)
        hasher.update(pyproject_content.encode("utf-8"))
        return hasher.hexdigest()

    def is_lockfile_hash_match(self) -> bool:
        if not self.lockfile_file.exists():
            return False
        hash_in_lockfile = str(self.lockfile["root"]["content_hash"])
        algo, hash_value = hash_in_lockfile.split(":")
        content_hash = self.get_content_hash(algo)
        return content_hash == hash_value

    def add_dependencies(
        self, requirements: Dict[str, Requirement], show_message: bool = True
    ) -> None:
        for name, dep in requirements.items():
            if dep.from_section == "default":
                deps = self.tool_settings["dependencies"]
            elif dep.from_section == "dev":
                deps = self.tool_settings["dev-dependencies"]
            else:
                section = f"{dep.from_section}-dependencies"
                if section not in self.tool_settings:
                    self.tool_settings[section] = tomlkit.table()
                deps = self.tool_settings[section]

            matched_name = next(
                filter(
                    lambda k: strip_extras(name)[0] == safe_name(k).lower(), deps.keys()
                ),
                None,
            )
            name_to_save = dep.name if matched_name is None else matched_name
            _, req_dict = dep.as_req_dict()
            if isinstance(req_dict, dict):
                req = tomlkit.inline_table()
                req.update(req_dict)
                req_dict = req
            deps[name_to_save] = req_dict
        self.write_pyproject(show_message)

    def write_pyproject(self, show_message: bool = True) -> None:
        with atomic_open_for_write(
            self.pyproject_file.as_posix(), encoding="utf-8"
        ) as f:
            f.write(tomlkit.dumps(self.pyproject))
        if show_message:
            stream.echo("Changes are written to pyproject.toml.")
        self._pyproject = None

    @property
    def meta(self) -> PackageMeta:
        return PackageMeta(self)

    def init_global_project(self) -> None:
        if not self.is_global:
            return
        if not self.pyproject_file.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            self.pyproject_file.write_text(
                """\
[tool.pdm.dependencies]
pip = "*"
setuptools = "*"
wheels = "*"

[tool.pdm.dev-dependencies]
"""
            )
            self._pyproject = None

    @property
    def cache_dir(self) -> Path:
        return Path(self.config.get("cache_dir"))

    def cache(self, name: str) -> Path:
        path = self.cache_dir / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def make_wheel_cache(self) -> shims.WheelCache:
        return shims.WheelCache(
            self.cache_dir.as_posix(), shims.FormatControl(set(), set())
        )

    def make_candidate_info_cache(self) -> CandidateInfoCache:

        python_hash = hashlib.sha1(
            str(self.environment.python_requires).encode()
        ).hexdigest()
        file_name = f"package_meta_{python_hash}.json"
        return CandidateInfoCache(self.cache_dir / file_name)

    def make_hash_cache(self) -> HashCache:
        return HashCache(directory=self.cache("hashes").as_posix())
