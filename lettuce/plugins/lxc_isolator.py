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
    p = subprocess.Popen(('%s ' % cmd) + ' '.join(args),
                         shell=True, stdout=subprocess.PIPE)
    out, err = p.communicate()
    return out, err, p.returncode


def lxc_command(cmd, *args):
    return system('lxc-%s' % cmd, *args)


container_rc_local_template = '''#!/bin/sh
%(exports)s
cd %(work_dir)s
/usr/local/bin/lettuce -s %(scenario)s %(feature_path)s > %(results_path)s
halt
'''


class LXCRunner(object):
    """
    Encapsulates scenario run in LXC container.
    Performs container setup, scenario run and result displaying
    """

    containers_path = '/var/lib/lxc/'
    default_container_name = os.environ.get('LETTUCE_LXC_DEFAULT', 'default')
    world_file_inner_path = '/root/world.dump'
    run_results_inner_path = '/root/run_results'
    run_results_str_regex = r'Scenario.*\n\n'
    run_results_stats_regex = r'step(|s) \(.*\)'

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

    def lxc_abs_path(self, path):
        if path.startswith('/'):
            path = path[1:]
        return os.path.join(self.container_rootfs, path)

    @property
    def scenario_index(self):
        scenario_list = self.scenario.feature.scenarios
        return scenario_list.index(self.scenario) + 1

    def run_scenario(self, scenario):
        self.scenario = scenario
        self.setup_container()
        world_path = self.lxc_abs_path(self.world_file_inner_path)
        self.save_world(world_path)
        self.run_container()
        self.wait_container()
        results = self.get_run_results()
        self.shutdown_container()
        self.display_results(results[0])
        return results[1]

    def create_container(self):
        self.container_name = self.get_free_container_name(self.default_container_name)
        lxc_command('clone', '-o %s -n %s' % (self.default_container_name,
                                              self.container_name))

    def setup_container(self):
        self.create_container()

        system('cp', '-rf',
               '/vagrant',
               self.lxc_abs_path('/vagrant'))
        container_working_dir = os.path.abspath('.')
        feature_path = sys.argv[-1]

        # setup start scripts
        # we assume that created container already have lettuce installed
        script_path = self.lxc_abs_path('/etc/rc.local')
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

    def save_world(self, filepath):
        with open(filepath, 'w') as f:
            for var in dir(world):
                if not var.startswith('_') and var not in ('absorb', 'spew'):
                    pickle.dump((var, world.__getattribute__(var)), f)

    def load_world(self, path):
        with open(path, 'r') as f:
            while True:
                try:
                    attr = pickle.load(f)
                    world.__setattr__(attr[0], attr[1])
                except EOFError:
                    break

    def run_container(self):
        return_code = lxc_command('start',
                                  '-d',
                                  '-n ' + self.container_name)[2]
        if return_code != 0:
            raise BaseException('Container failed to start')

    def wait_container(self):
        """
        Waits for run_results_inner_path with /proc/x poll.
        """

        while True:
            ps_out = system('ps', 'auxf')[0]

            match = re.search(r'-n %s.*' % self.container_name,
                              ps_out,
                              re.DOTALL)
            if not match:
                return

            match = re.search(r'.*lettuce.*', match.group())
            if match:
                lxc_lettuce_pid = match.group().split()[1]
                while os.path.exists('/proc/%s' % lxc_lettuce_pid):
                    sleep(1)
                return
            sleep(1)

    def get_run_results(self):
        """
        Reads file on run_results_inner_path.
        Returns pair with string representation of step run result
        and tuple of number failed, skipped and passed steps
        """
        path = self.lxc_abs_path(self.run_results_inner_path)
        with open(path, 'r') as fp:
            lettuce_out = fp.read()
            match = re.search(self.run_results_str_regex,
                              lettuce_out,
                              re.DOTALL)
            results = match.group()
            second_line_start = results.index('\n') + 1

            run_result_str = results[second_line_start:].strip()

            # statistics
            match = re.search(self.run_results_stats_regex, lettuce_out)
            stats = match.group()

            def _get_steps_num(type_):
                match = re.search(r'\d+ %s' % type_, stats)
                if not match:
                    return 0
                match_str = match.group()
                return int(match_str.split()[0])

            failed_num = _get_steps_num('failed')
            skipped_num = _get_steps_num('skipped')
            passed_num = _get_steps_num('passed')

            stats_tuple = (failed_num, skipped_num, passed_num)
            return (run_result_str, stats_tuple)

    def shutdown_container(self):
        # lxc_command('stop', '-n ' + self.container_name)
        lxc_command('destroy', '-n ' + self.container_name)

    def display_results(self, results):
        # just print results... or use some output plugin
        print results


lxc_runner = LXCRunner()


@before.each_scenario
def handle_lxc_tag_setup(scenario):
    if LXC_RUNNER_TAG in scenario.tags:
        # if world dump file is presented, lettuce is runned in LXC
        # so we need to restore world
        if os.path.exists(lxc_runner.world_file_inner_path):
            lxc_runner.load_world(lxc_runner.world_file_inner_path)
            return

        for step in scenario.steps:
            step.passed = True
            step.run = lambda *args, **kwargs: True
            step.ran = True

        lxc_runner.saved_runall = core.Step.run_all

        def run_all_mock(*args, **kwargs):
            failed, skipped, passed = lxc_runner.run_scenario(scenario)
            return (scenario.steps,  # all
                    scenario.steps[:passed],  # passed
                    scenario.steps[passed:passed + failed],  # failed
                    [],              # undefined
                    [])              # reasons to fail

        core.Step.run_all = staticmethod(run_all_mock)


@after.each_scenario
def handle_lxc_tag_teardown(scenario):
    if LXC_RUNNER_TAG in scenario.tags and not os.path.exists(lxc_runner.world_file_inner_path):
        core.Step.run_all = staticmethod(lxc_runner.saved_runall)
