import io
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import install_dependencies as installer


class BuildInstallTests(unittest.TestCase):
    def run_main(self, codes):
        results = [subprocess.CompletedProcess([], code) for code in codes]
        with patch.object(installer.subprocess, 'run', side_effect=results) as run, patch.object(installer.time, 'sleep') as sleep, patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO):
            code = installer.main()
        return code, run, sleep

    def test_success_upgrades_pip_installs_requirements_and_checks_environment(self):
        code, run, sleep = self.run_main([0, 0, 0])
        self.assertEqual(code, 0)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertTrue(all(cmd[0] == sys.executable for cmd in commands))
        self.assertIn('pip>=25.1', commands[0])
        self.assertIn('--no-cache-dir', commands[0])
        self.assertIn('--no-cache-dir', commands[1])
        self.assertIn('--resume-retries', commands[1])
        self.assertEqual(Path(commands[1][-1]), installer.ROOT / 'requirements.txt')
        self.assertEqual(commands[2], [sys.executable, '-m', 'pip', 'check'])
        self.assertNotIn('--trusted-host', commands[1])
        sleep.assert_not_called()

    def test_failed_index_fetch_retries_entire_install_command(self):
        code, run, sleep = self.run_main([0, 2, 2, 0, 0])
        self.assertEqual(code, 0)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[1], commands[2])
        self.assertEqual(commands[2], commands[3])
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 10])

    def test_permanent_install_failure_preserves_failure_exit_code(self):
        code, run, sleep = self.run_main([0, 2, 2, 2])
        self.assertEqual(code, 2)
        self.assertEqual(run.call_count, 4)
        self.assertEqual(sleep.call_count, 2)

    def test_pip_upgrade_failure_stops_before_installing_bot_dependencies(self):
        code, run, sleep = self.run_main([1, 1, 1])
        self.assertEqual(code, 1)
        self.assertEqual(run.call_count, 3)
        self.assertTrue(all('pip>=25.1' in call.args[0] for call in run.call_args_list))

    def test_dependency_conflicts_fail_the_build(self):
        code, run, sleep = self.run_main([0, 0, 1])
        self.assertEqual(code, 1)
        sleep.assert_not_called()

    def test_nixpacks_install_uses_the_virtualenv_interpreter(self):
        with (installer.ROOT / 'nixpacks.toml').open('rb') as handle:
            config = tomllib.load(handle)
        self.assertEqual(config['phases']['install']['cmds'], [
            'python -m venv --copies /opt/venv',
            '/opt/venv/bin/python scripts/install_dependencies.py',
        ])
        self.assertIn('/opt/venv/bin', config['phases']['install']['paths'])
