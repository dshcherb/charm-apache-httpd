#!/usr/bin/env python3

import subprocess
import collections
import yaml
import base64
import logging
import sys

sys.path.append('lib') # noqa

from ops.charm import CharmBase, CharmEvents
from ops.framework import (
    EventSource,
    EventBase,
    StoredState,
)

from ops.model import ActiveStatus

from ops.main import main

from pathlib import Path

from enum import (
    Enum,
    unique,
)

logger = logging.getLogger(__name__)


class ApacheReadyEvent(EventBase):
    pass


class ApacheCharmEvents(CharmEvents):
    apache_ready = EventSource(ApacheReadyEvent)


class ApacheModuleEnableException(Exception):
    pass


class ApacheModuleDisableException(Exception):
    pass


class ApacheSiteEnableException(Exception):
    pass


class ApacheSiteDisableException(Exception):
    pass


class SystemctlCommandException(Exception):
    pass


@unique
class ModuleState(Enum):
    """a2query exit codes per apache2/debian/a2query.in

    a2query only considers modules that it knows about from the state in /var/lib/apache2/module/ directory.

    """
    Found = 0
    NotFound = 1
    OffByAdmin = 32
    OffByMaintainer = 33


class Charm(CharmBase):

    on = ApacheCharmEvents()

    HTTPD_SERVICE_NAME = 'apache2'

    state = StoredState()

    SYSTEMCTL_COMMANDS = {'start', 'stop', 'restart', 'reload', 'daemon-reload', 'disable', 'enable'}

    APACHE_CONFIG_DIR = Path('/etc/apache2')

    def __init__(self, *args):
        super().__init__(*args)

        self.framework.observe(self.on.install, self)
        self.framework.observe(self.on.start, self)
        self.framework.observe(self.on.stop, self)
        self.framework.observe(self.on.config_changed, self)
        self.framework.observe(self.on.vhost_config_relation_changed, self)
        self.framework.observe(self.on.apache_ready, self)

    def on_install(self, event):
        # Initialize Charm state
        self.state._current_modules = set()

        logger.info(f'on_install: installing {self.HTTPD_SERVICE_NAME}')
        self.apt_install(['apache2'])
        # Disable vhosts that come from the default site.
        self._disable_site('000-default.conf')

    def on_start(self, event):
        logger.info(f'on_start: starting {self.HTTPD_SERVICE_NAME}')
        self._systemd_unit_command('start', self.HTTPD_SERVICE_NAME)

    def on_stop(self, event):
        logger.info(f'on_stop: stop {self.HTTPD_SERVICE_NAME}')
        self._systemd_unit_command('stop', self.HTTPD_SERVICE_NAME)

    def on_config_changed(self, event):
        logger.info(f'on_config_changed: Updating {self.HTTPD_SERVICE_NAME} configuration.')

        config_modules = set(self.framework.model.config['modules'].split())

        modules_to_disable = self.state._current_modules - config_modules

        changed_modules = self.state._current_modules ^ config_modules

        for m in modules_to_disable:
            self._disable_module(m)
            self.state._current_modules.remove(m)

        for m in config_modules:
            self._enable_module(m)
            self.state._current_modules.add(m)

        # Modules were either enabled or disabled so a restart is needed.
        if changed_modules:
            self._systemd_unit_command('restart', self.HTTPD_SERVICE_NAME)

        self._assess_readiness()

    def on_apache_ready(self, event):
        self.framework.model.unit.status = ActiveStatus()

    def _assess_readiness(self):
        if self._is_systemd_unit_active(self.HTTPD_SERVICE_NAME):
            self.state.ready = True
            self.on.apache_ready.emit()
        else:
            self.state.ready = False

    def _systemd_unit_command(self, command, name):
        """Run a systemctl command from a subset of commands on a systemd unit."""
        if command in self.SYSTEMCTL_COMMANDS:
            logger.info(f'Running systemctl {command} {name}')
            rc = subprocess.call(['systemctl', command, name])
            if rc:
                raise SystemctlCommandException(f'got unexpected return code {rc} while executing {command} on unit {name}')
        else:
            raise NotImplementedError(f'usage of systemctl command "{command}" is not supported by the charm.')

    def _is_systemd_unit_active(self, name):
        """Run systemctl is-active on a unit.

        is-active returns a non-zero exit code to represent an inactive service.
        """
        rc = subprocess.call(['systemctl', 'is-active', name])
        return False if rc else True

    def on_vhost_config_relation_changed(self, event):
        if not self.state.ready:
            event.defer()
            return

        # no subordinate unit observed yet, let's wait until it appears.
        if not event.unit:
            return

        vhosts_serialized = event.relation.data[event.app].get('vhosts')
        # No vhosts are provided yet - skip this event.
        if not vhosts_serialized:
            return

        vhosts = yaml.safe_load(vhosts_serialized)
        for vhost in vhosts:
            self._enable_site(self.create_vhost(self.framework.model.config['server_name'], vhost["template"], vhost['port']))

        self._systemd_unit_command('reload', self.HTTPD_SERVICE_NAME)

    def _get_module_state(self, module_name):
        return ModuleState(subprocess.call(['a2query', '-m', module_name]))

    def _disable_module(self, module_name):
        module_state = self._get_module_state(module_name)
        if module_state == ModuleState.Found:
            try:
                logger.info(f'Disabling apache2 module {module_name}')
                subprocess.check_call(['a2dismod', module_name])
            except subprocess.CalledProcessError:
                raise ApacheModuleDisableException(f'unable to disable apache2 module {module_name}.')
        elif module_state in (ModuleState.OffByAdmin, ModuleState.OffByMaintainer):
            logger.info(f'Apache2 module {module_name} is already disabled.')
        elif module_state == ModuleState.NotFound:
            raise ApacheModuleDisableException(f'module {module_name} was not found.')
        else:
            raise ApacheModuleDisableException(f'unexpected module {module_name} state.')

    def _enable_module(self, module_name):
        try:
            logger.info(f'Enabling apache2 module {module_name}')
            subprocess.check_call(['a2enmod', module_name])
        except subprocess.CalledProcessError:
            raise ApacheModuleEnableException(f'unable to enable apache2 module {module_name}.')

    def _enable_site(self, site_name):
        try:
            logger.info(f'Enabling site {site_name}')
            subprocess.check_call(['a2ensite', site_name])
        except subprocess.CalledProcessError:
            raise ApacheSiteEnableException(f'unable to enable apache2 site {site_name}.')

    def _disable_site(self, site_name):
        try:
            logger.info(f'Disabling site {site_name}')
            subprocess.check_call(['a2dissite', site_name])
        except subprocess.CalledProcessError:
            raise ApacheSiteDisableException(f'unable to disable apache2 site {site_name}.')

    @classmethod
    def create_vhost(cls, server_name, template, port, protocol=None):
        """
        Create and enable a vhost in apache.

        server_name -- the server name to use for a vhost.
        template -- the template string to use.
        port -- port on which to listen (int)
        protocol -- used to name the vhost file intelligently. If not specified the port will be used instead (ex: http, https).

        return -- vhost file name.
        """
        if protocol is None:
            protocol = str(port)

        template = base64.b64decode(template).decode('utf-8')

        vhost_name = f'{server_name}_{protocol}'
        vhost_file = cls.APACHE_CONFIG_DIR / 'sites-available' / f'{vhost_name}.conf'

        logger.info(f'Writing vhost config to {vhost_file}')
        vhost_file.write_text(template)

        return vhost_name

    def apt_install(self, packages, options=None):
        """Install one or more packages.

        packages -- package(s) to install.
        options -- a list of apt options to use.
        """
        if options is None:
            options = ['--option=Dpkg::Options::=--force-confold']

        command = ['apt-get', '--assume-yes']
        command.extend(options)
        command.append('install')

        if isinstance(packages, collections.abc.Sequence):
            command.extend(packages)
        else:
            raise ValueError(f'Invalid type was used for the "packages" argument: {type(packages)} instead of str')

        logger.info("Installing {} with options: {}".format(packages, options))

        subprocess.check_call(command)


if __name__ == '__main__':
    main(Charm)
