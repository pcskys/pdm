from __future__ import annotations

import importlib
import pkgutil
import sys
from typing import Optional, Type

import click
import pkg_resources

from pdm import __version__
from pdm.cli.commands.base import BaseCommand
from pdm.cli.options import verbose_option
from pdm.cli.utils import PdmFormatter, PdmParser
from pdm.installers import Synchronizer
from pdm.iostream import stream
from pdm.models.repositories import PyPIRepository
from pdm.project import Project
from pdm.project.config import Config, ConfigItem
from pdm.resolver import Resolver

COMMANDS_MODULE_PATH = importlib.import_module("pdm.cli.commands").__path__


class Core:
    """A high level object that manages all classes and configurations
    """

    def __init__(self):
        self.version = __version__

        self.project_class = Project
        self.repository_class = PyPIRepository
        self.resolver_class = Resolver
        self.synchronizer_class = Synchronizer

        self.parser = None
        self.subparsers = None

    def init_parser(self):
        self.parser = PdmParser(
            prog="pdm",
            description="PDM - Python Development Master",
            formatter_class=PdmFormatter,
        )
        self.parser.is_root = True
        self.parser.add_argument(
            "-V",
            "--version",
            action="version",
            version="{}, version {}".format(
                click.style("pdm", bold=True), self.version
            ),
            help="show the version and exit",
        )
        verbose_option.add_to_parser(self.parser)

        self.subparsers = self.parser.add_subparsers()
        for _, name, _ in pkgutil.iter_modules(COMMANDS_MODULE_PATH):
            module = importlib.import_module(f"pdm.cli.commands.{name}", __name__)
            try:
                klass = module.Command  # type: Type[BaseCommand]
            except AttributeError:
                continue
            self.register_command(klass, klass.name or name)

    def __call__(self, *args, **kwargs):
        return self.main(*args, **kwargs)

    def main(self, args=None, prog_name=None, obj=None, **extra):
        """The main entry function"""
        self.init_parser()
        self.load_plugins()

        self.parser.set_defaults(global_project=None)
        options = self.parser.parse_args(args or None)
        stream.set_verbosity(options.verbose)

        if obj is not None:
            options.project = obj
        if options.global_project:
            options.project = options.global_project
        if not getattr(options, "project", None):
            options.project = self.project_class()

        # Add reverse reference for core object
        options.project.core = self

        try:
            f = options.handler
        except AttributeError:
            self.parser.print_help()
            sys.exit(1)
        else:
            try:
                f(options.project, options)
            except Exception:
                etype, err, traceback = sys.exc_info()
                if stream.verbosity > stream.NORMAL:
                    raise err.with_traceback(traceback)
                stream.echo("[{}]: {}".format(etype.__name__, err), err=True)
                sys.exit(1)

    def register_command(
        self, command: Type[BaseCommand], name: Optional[str] = None
    ) -> None:
        """Register a subcommand to the subparsers,
        with an optional name of the subcommand.
        """
        command.project_class = self.project_class
        command.register_to(self.subparsers, name)

    @staticmethod
    def add_config(name: str, config_item: ConfigItem) -> None:
        """Add a config item to the configuration class"""
        Config.add_config(name, config_item)

    def load_plugins(self):
        """Import and load plugins under `pdm.plugin` namespace
        A plugin is a callable that accepts the core object as the only argument.

        :Example:

        def my_plugin(core: pdm.core.Core) -> None:
            ...

        """
        for plugin in pkg_resources.iter_entry_points("pdm.plugin"):
            plugin.load()(self)


# the main object, which can also act as a callable
main = Core()
