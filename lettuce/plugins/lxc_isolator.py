import os
import pickle
import subprocess
import sys
import re
from time import sleep

from lettuce import core
from lettuce import world
from lettuce.terrain import after
from lettuce.terrain import before


LXC_RUNNER_TAG = 'lxc'


def system(cmd, *args):
    p = subprocess.Popen(('%s ' % cmd) + ' '.join(args), shell=True)
    out, err = p.communicate()
    return out, err, p.returncode


def lxc_command(cmd, *args):
    return system('lxc-%s' % cmd, *args)


container_rc_local_template = '''#!/bin/sh
%(exports)s
cd %(work_dir)s
/usr/local/bin/lettuce -s %(scenario)s %(feature_path)s > /%(results_path)s
exit 0
'''


class LXCRunner(object):
    """
    Encapsulates scenario run in LXC container.
    Performs container setup, scenario run and result displaying
    """
    containers_path = '/var/lib/lxc/'
    default_container_name = os.environ.get('LETTUCE_LXC_DEFAULT', None) or \
        'default'
    env_file_inner_path = 'root/env.dump'
    world_file_inner_path = 'root/world.dump'
    run_results_inner_path = 'root/run_results'
    run_results_search_regex = r'Scenario.*\n\n'

    def get_free_container_name(self, prefix):
        """
        Iterate over containers_path and find free container name
        with format '<prefix>.<num>' where num is in range of 00000-99999
        """

        containers = filter(lambda x:
                            x.startswith(self.default_container_name + '.'),
                            os.listdir(self.containers_path))
        containers.sort()
        container_num = 0
        if containers:
            container_num = int(containers[-1].split('.')[1]) + 1
        return '%s.%05d' % (self.default_container_name, container_num)

    def __init__(self):
        super(LXCRunner, self).__init__()
        self.saved_runall = None
        self.container_name = None
        self.scenario = None

    @property
    def container_rootfs(self):
        if not self.container_name:
            return None
        return os.path.join(self.containers_path,
                            self.container_name,
                            'rootfs')

    def inner_path_to_global(self, path):
        return os.path.join(self.container_rootfs, path)

    @property
    def scenario_index(self):
        scenario_list = self.scenario.feature.scenarios
        return scenario_list.index(self.scenario) + 1

    def run_scenario(self, scenario):
        self.scenario = scenario
        self.setup_container()
        # env_path = self.inner_path_to_global(self.env_file_inner_path)
        # self.save_env(env_path)
        world_path = self.inner_path_to_global(self.world_file_inner_path)
        self.save_world(world_path)
        self.run_container()
        self.wait_container()
        results = self.get_run_results()
        self.shutdown_container()
        self.display_results(results)

    def create_container(self):
        self.container_name = self.get_free_container_name(self.default_container_name)
        lxc_command('clone', '-o %s -n %s' % (self.default_container_name,
                                              self.container_name))

    def setup_container(self):
        self.create_container()

        # actual setup
        system('cp', '-rf',
               '/vagrant',
               self.inner_path_to_global('root/vagrant'))
        working_dir = os.path.abspath('.')
        container_working_dir = working_dir.replace('/vagrant',
                                                    '/root/vagrant')

        feature_path = sys.argv[1].replace('/vagrant', '/root/vagrant')

        # setup start scripts
        # we assume that created container already have lettuce installed
        script_path = self.inner_path_to_global('etc/rc.local')
        with open(script_path, 'w') as fp:
            def _export_env_var(acc, keyvalue):
                return "%sexport %s='%s'\n" % ((acc,) + keyvalue)

            export_str = reduce(_export_env_var, os.environ.items(), '')

            fp.write(container_rc_local_template % {
                     'exports': export_str,
                     'work_dir': container_working_dir,
                     'scenario': self.scenario_index,
                     'feature_path': feature_path,
                     'results_path': self.run_results_inner_path})
            os.chmod(script_path, 0755)

        # TODO: restore world

    # def save_env(self, filepath):
    #     with open(filepath, 'w') as fp:
    #         pickle.dump(os.environ, fp)

    def save_world(self, filepath):
        with open(filepath, 'w') as f:
            for var in dir(world):
                if not var.startswith('_') and var not in ('absorb', 'spew'):
                    pickle.dump(world.__getattribute__(var), f)

    def run_container(self):
        lxc_command('start', '-d', '-n ' + self.container_name)

    def wait_container(self):
        """
        Waits for run_results_inner_path.

        This file can be created by lettuce, or run script if lettuce
        fails while running.
        """
        while not os.path.exists(self.inner_path_to_global(self.run_results_inner_path)):
            sleep(1)

    def get_run_results(self):
        """
        Reads file on run_results_inner_path.
        """
        path = self.inner_path_to_global(self.run_results_inner_path)
        with open(path, 'r') as fp:
            lettuce_out = fp.read()
            # print 'lettuce out:\n %s' % lettuce_out
            match = re.search(self.run_results_search_regex,
                              lettuce_out,
                              re.DOTALL)
            results = match.group()
            second_line_start = results.index('\n') + 1
            return results[second_line_start:].strip()

    def shutdown_container(self):
        lxc_command('stop', '-n ' + self.container_name)
        # lxc_command('destroy', '-n ' + self.container_name)

    def display_results(self, results):
        # just print results... or use some output plugin
        print results


lxc_runner = LXCRunner()


@before.each_scenario
def handle_lxc_tag_setup(scenario):
    if LXC_RUNNER_TAG in scenario.tags:
        for step in scenario.steps:
            step.passed = True
            step.run = lambda *args, **kwargs: True
            step.ran = True

        lxc_runner.saved_runall = core.Step.run_all

        def run_all_mock(*args, **kwargs):
            lxc_runner.run_scenario(scenario)
            return (scenario.steps,  # all
                    scenario.steps,  # passed
                    [],              # failed
                    [],              # undefined
                    [])              # reasons to fail

        core.Step.run_all = staticmethod(run_all_mock)

        scenario.run = lambda ignore_case, failfast=False: \
            core.ScenarioResult(scenario,
                                scenario.steps,  # passed
                                [],              # failed
                                [],              # skipped
                                [])              # undefined


@after.each_scenario
def handle_lxc_tag_teardown(scenario):
    if LXC_RUNNER_TAG in scenario.tags and lxc_runner:
        core.Step.run_all = staticmethod(lxc_runner.saved_runall)
