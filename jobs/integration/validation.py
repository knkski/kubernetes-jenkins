import asyncio
import json
import os
import requests
import traceback
import yaml
import re
import random

from asyncio_extras import async_contextmanager
from async_generator import yield_
from datetime import datetime
from .logger import log, log_calls, log_calls_async
from pprint import pformat
from tempfile import NamedTemporaryFile
from .utils import (
    assert_no_unit_errors,
    asyncify,
    timeout_for_current_task,
    retry_async_with_timeout,
    arch,
    scp_to,
    scp_from
)

from .base import (
    _juju_wait,
    _model_from_env,
    _controller_from_env)


@log_calls_async
async def validate_all(model, log_dir):
    cpu_arch = await asyncify(arch)()
    validate_status_messages(model)
    await validate_snap_versions(model)
    await validate_gpu_support(model)
    if cpu_arch not in ['s390x', 'arm64', 'aarch64']:
        await validate_dashboard(model, log_dir)
    await validate_kubelet_anonymous_auth_disabled(model)
    await validate_rbac_flag(model)
    await validate_rbac(model)
    if cpu_arch not in ['s390x', 'arm64', 'aarch64']:
        await validate_microbot(model)
        await validate_docker_logins(model)
    await validate_worker_master_removal(model)
    await validate_sans(model)
    if any(app in model.applications for app in ('canal', 'calico')):
        log("Running network policy specific tests")
        await validate_network_policies(model)
    await validate_extra_args(model)
    await validate_kubelet_extra_config(model)
    await validate_audit_default_config(model)
    await validate_audit_empty_policy(model)
    await validate_audit_custom_policy(model)
    await validate_audit_webhook(model)
    if cpu_arch not in ['s390x', 'arm64', 'aarch64']:
        await validate_keystone(model)
    assert_no_unit_errors(model)
    if ('vault' in model.applications and
            cpu_arch not in ['s390x', 'arm64', 'aarch64']):
        await validate_encryption_at_rest(model)


@log_calls
def validate_status_messages(model):
    ''' Validate that the status messages are correct. '''
    expected_messages = {
        'kubernetes-master': 'Kubernetes master running.',
        'kubernetes-worker': 'Kubernetes worker running.',
    }
    for app, message in expected_messages.items():
        for unit in model.applications[app].units:
            assert unit.workload_status_message == message


@log_calls_async
async def validate_snap_versions(model):
    ''' Validate that the installed snap versions are consistent with channel
    config on the charms.
    '''
    snaps_to_validate = {
        'kubernetes-master': [
            'kubectl',
            'kube-apiserver',
            'kube-controller-manager',
            'kube-scheduler',
            'cdk-addons',
        ],
        'kubernetes-worker': [
            'kubectl',
            'kubelet',
            'kube-proxy',
        ],
    }
    for app_name, snaps in snaps_to_validate.items():
        app = model.applications[app_name]
        config = await app.get_config()
        channel = config['channel']['value']
        if '/' not in channel:
            message = 'validate_snap_versions: skipping %s, channel=%s'
            message = message % (app_name, channel)
            log(message)
            continue
        track = channel.split('/')[0]
        for unit in app.units:
            action = await unit.run('snap list')
            assert action.status == 'completed'
            raw_output = action.data['results']['Stdout']
            # Example of the `snap list` output format we're expecting:
            # Name        Version  Rev   Developer  Notes
            # conjure-up  2.1.5    352   canonical  classic
            # core        16-2     1689  canonical  -
            # kubectl     1.6.2    27    canonical  classic
            lines = raw_output.splitlines()[1:]
            snap_versions = dict(line.split()[:2] for line in lines)
            for snap in snaps:
                snap_version = snap_versions[snap]
                if not snap_version.startswith(track + '.'):
                    log("Snap {} is version {} and not {}".format(snap, snap_version, track + '.'))
                assert snap_version.startswith(track + '.')


@log_calls_async
async def validate_rbac(model):
    ''' Validate RBAC is actually on '''
    app = model.applications['kubernetes-master']
    await app.set_config({'authorization-mode': 'RBAC,Node'})
    await wait_for_process(model, 'RBAC')
    cmd = "/snap/bin/kubectl --kubeconfig /root/cdk/kubeconfig get clusterroles"
    worker = model.applications['kubernetes-worker'].units[0]
    output = await worker.run(cmd)
    assert output.status == 'completed'
    assert "forbidden" in output.data['results']['Stderr'].lower()
    await app.set_config({'authorization-mode': 'AlwaysAllow'})
    await wait_for_process(model, 'AlwaysAllow')
    output = await worker.run(cmd)
    assert output.status == 'completed'
    assert "forbidden" not in output.data['results']['Stderr']


@log_calls_async
async def validate_rbac_flag(model):
    ''' Switch between auth modes and check the apiserver follows '''
    master = model.applications['kubernetes-master']
    await master.set_config({'authorization-mode': 'RBAC'})
    await wait_for_process(model, 'RBAC')
    await master.set_config({'authorization-mode': 'AlwaysAllow'})
    await wait_for_process(model, 'AlwaysAllow')


@log_calls_async
async def wait_for_process(model, arg):
    ''' Retry api_server_with_arg <checks> times with a 5 sec interval '''
    checks = 10
    ready = False
    while not ready:
        checks -= 1
        if await api_server_with_arg(model, arg):
            return
        else:
            if checks <= 0:
                assert False
            await asyncio.sleep(5)


@log_calls_async
async def wait_for_not_process(model, arg):
    ''' Retry api_server_with_arg <checks> times with a 5 sec interval '''
    checks = 10
    ready = False
    while not ready:
        checks -= 1
        if await api_server_with_arg(model, arg):
            if checks <= 0:
                assert False
            await asyncio.sleep(5)
        else:
            return


async def api_server_with_arg(model, argument):
    master = model.applications['kubernetes-master']
    for unit in master.units:
        search = 'ps -ef | grep {} | grep apiserver'.format(argument)
        action = await unit.run(search)
        assert action.status == 'completed'
        raw_output = action.data['results']['Stdout']
        if len(raw_output.splitlines()) != 1:
            return False
    return True


@log_calls_async
async def validate_microbot(model):
    ''' Validate the microbot action '''
    unit = model.applications['kubernetes-worker'].units[0]
    action = await unit.run_action('microbot', delete=True)
    await action.wait()
    action = await unit.run_action('microbot', replicas=3)
    await action.wait()
    assert action.status == 'completed'
    for i in range(60):
        try:
            resp = await asyncify(
                requests.get)('http://' + action.data['results']['address'])
            if resp.status_code == 200:
                return
        except requests.exceptions.ConnectionError:
            log(
                "Caught connection error attempting to hit xip.io, "
                "retrying. Error follows:")
            traceback.print_exc()
        await asyncio.sleep(1)
    raise MicrobotError('Microbot failed to start.')


@log_calls_async
async def validate_dashboard(model, log_dir):
    ''' Validate that the dashboard is operational '''
    unit = model.applications['kubernetes-master'].units[0]
    with NamedTemporaryFile() as f:
        await scp_from(unit, 'config', f.name)
        with open(f.name, 'r') as stream:
            config = yaml.load(stream)
    url = config['clusters'][0]['cluster']['server']
    user = config['users'][0]['user']['username']
    password = config['users'][0]['user']['password']
    auth = requests.auth.HTTPBasicAuth(user, password)
    resp = await asyncify(requests.get)(url, auth=auth, verify=False)
    assert resp.status_code == 200
    # get k8s version
    app_config = await model.applications['kubernetes-master'].get_config()
    channel = app_config['channel']['value']
    # if we do not detect the version from the channel eg edge, stable etc
    # we should default to the latest dashboard url format
    k8s_version = (2, 0)
    if '/' in channel:
        version_string = channel.split('/')[0]
        k8s_version = tuple(int(q) for q in re.findall("[0-9]+", version_string)[:2])

    # dashboard will present a login form prompting for login
    if k8s_version < (1, 8):
        url = '%s/api/v1/namespaces/kube-system/services/kubernetes-dashboard/proxy/#!/login'
    else:
        url = '%s/api/v1/namespaces/kube-system/services/https:kubernetes-dashboard:/proxy/#!/login'
    url %= config['clusters'][0]['cluster']['server']

    log('Waiting for dashboard to stabilize...')

    async def dashboard_present(url):
        resp = await asyncify(requests.get)(url, auth=auth, verify=False)
        if resp.status_code == 200 and "Dashboard" in resp.text:
            return True
        return False

    await retry_async_with_timeout(verify_ready,
                                   (unit, 'po', ['kubernetes-dashboard'], '-n kube-system'),
                                   timeout_msg="Unable to find kubernetes dashboard before timeout")

    await retry_async_with_timeout(dashboard_present, (url,),
                                   timeout_msg="Unable to reach dashboard")


@log_calls_async
async def validate_kubelet_anonymous_auth_disabled(model):
    ''' Validate that kubelet has anonymous auth disabled '''
    @log_calls_async
    async def validate_unit(unit):
        await unit.run('open-port 10250')
        address = unit.public_address
        url = 'https://%s:10250/pods/' % address
        response = await asyncify(requests.get)(url, verify=False)
        assert response.status_code == 401  # Unauthorized
    units = model.applications['kubernetes-worker'].units
    await asyncio.gather(*(validate_unit(unit) for unit in units))


async def verify_deleted(unit, entity_type, name, extra_args=''):
    cmd = "/snap/bin/kubectl {} --output json get {}".format(extra_args, entity_type)
    output = await unit.run(cmd)
    if 'error' in output.results['Stdout']:
        # error resource type not found most likely. This can happen when the api server is
        # restarting. As such, don't assume this means we've finished the deletion
        return False
    out_list = json.loads(output.results['Stdout'])
    for item in out_list['items']:
        if item['metadata']['name'] == name:
            return False
    return True


# note that name_list is a list of entities(pods, services, etc) being searched
# and that partial matches work. If you have a pod with random characters at the
# end due to being in a deploymnet, you can add just the name of the deployment
# and it will still match
async def verify_ready(unit, entity_type, name_list, extra_args=''):
    cmd = "/snap/bin/kubectl {} --output json get {}".format(extra_args, entity_type)
    output = await unit.run(cmd)
    if 'error' in output.results['Stdout']:
        # error resource type not found most likely. This can happen when the api server is
        # restarting. As such, don't assume this means ready.
        return False
    out_list = json.loads(output.results['Stdout'])

    for name in name_list:
        # find all entries that match this
        matches = [n for n in out_list['items'] if name in n['metadata']['name']]

        # now verify they are ALL ready, it isn't cool if just one is ready now
        ready = [n for n in matches if n['kind'] == 'DaemonSet' or
                 n['status']['phase'] == 'Running' or
                 n['status']['phase'] == 'Active']
        if len(ready) != len(matches):
            return False

    # made it here then all the matches are ready
    return True


@log_calls_async
async def validate_network_policies(model):
    ''' Apply network policy and use two busyboxes to validate it. '''
    here = os.path.dirname(os.path.abspath(__file__))
    unit = model.applications['kubernetes-master'].units[0]

    # Clean-up namespace from any previous runs.
    cmd = await unit.run('/snap/bin/kubectl delete ns netpolicy')
    assert cmd.status == 'completed'
    log('Waiting for pods to finish terminating...')

    await retry_async_with_timeout(verify_deleted,
                                   (unit, 'ns', 'netpolicy'),
                                   timeout_msg="Unable to remove the namespace netpolicy")

    # Move manifests to the master
    await scp_to(os.path.join(here, "templates", "netpolicy-test.yaml"), unit, "netpolicy-test.yaml")
    await scp_to(os.path.join(here, "templates", "restrict.yaml"), unit, "restrict.yaml")
    cmd = await unit.run('/snap/bin/kubectl create -f /home/ubuntu/netpolicy-test.yaml')
    if not cmd.results['Code'] == '0':
        log('Failed to create netpolicy test!')
        log(cmd.results)
    assert cmd.status == 'completed' and cmd.results['Code'] == '0'
    log('Waiting for pods to show up...')
    await retry_async_with_timeout(verify_ready,
                                   (unit, 'po', ['bboxgood', 'bboxbad'], '-n netpolicy'),
                                   timeout_msg="Unable to create pods for network policy test")

    # Try to get to nginx from both busyboxes.
    # We expect no failures since we have not applied the policy yet.
    async def get_to_networkpolicy_service():
        log("Reaching out to nginx.netpolicy with no restrictions")
        query_from_bad = "/snap/bin/kubectl exec bboxbad -n netpolicy -- wget --timeout=30  nginx.netpolicy"
        query_from_good = "/snap/bin/kubectl exec bboxgood -n netpolicy -- wget --timeout=30  nginx.netpolicy"
        cmd_good = await unit.run(query_from_good)
        cmd_bad = await unit.run(query_from_bad)
        if (cmd_good.status == 'completed' and
                cmd_bad.status == 'completed' and
                "index.html" in cmd_good.data['results']['Stderr'] and
                "index.html" in cmd_bad.data['results']['Stderr']):
            return True
        return False

    await retry_async_with_timeout(get_to_networkpolicy_service, (),
                                   timeout_msg="Failed to query nginx.netpolicy even before applying restrictions")


    # Apply network policy and retry getting to nginx.
    # This time the policy should block us.
    cmd = await unit.run('/snap/bin/kubectl create -f /home/ubuntu/restrict.yaml')
    assert cmd.status == 'completed'
    await asyncio.sleep(10)

    async def get_to_restricted_networkpolicy_service():
        log("Reaching out to nginx.netpolicy with restrictions")
        query_from_bad="/snap/bin/kubectl exec bboxbad -n netpolicy -- wget --timeout=30  nginx.netpolicy -O foo.html"
        query_from_good = "/snap/bin/kubectl exec bboxgood -n netpolicy -- wget --timeout=30  nginx.netpolicy -O foo.html"
        cmd_good = await unit.run(query_from_good)
        cmd_bad = await unit.run(query_from_bad)
        if (cmd_good.status == 'completed' and
                cmd_bad.status == 'completed' and
                "foo.html" in cmd_good.data['results']['Stderr'] and
                "timed out" in cmd_bad.data['results']['Stderr']):
            return True
        return False

    await retry_async_with_timeout(get_to_restricted_networkpolicy_service, (),
                                   timeout_msg="Failed query restricted nginx.netpolicy")

    # Clean-up namespace from next runs.
    cmd = await unit.run('/snap/bin/kubectl delete ns netpolicy')
    assert cmd.status == 'completed'


@log_calls_async
async def validate_worker_master_removal(model):
    # Add a second master
    masters = model.applications['kubernetes-master']
    unit_count = len(masters.units)
    if unit_count < 2:
        await masters.add_unit(1)
    await asyncify(_juju_wait)()

    # Add a second worker
    workers = model.applications['kubernetes-worker']
    unit_count = len(workers.units)
    if unit_count < 2:
        await workers.add_unit(1)
    await asyncify(_juju_wait)()
    unit_count = len(workers.units)

    # Remove a worker to see how the masters handle it
    await workers.units[0].remove()
    while len(workers.units) == unit_count:
        await asyncio.sleep(3)
        log('Waiting for worker removal.')
        assert_no_unit_errors(model)
    await asyncify(_juju_wait)()

    # Remove the master leader
    unit_count = len(masters.units)
    for master in masters.units:
        if await master.is_leader_from_status():
            await master.remove()
    while len(masters.units) == unit_count:
        await asyncio.sleep(3)
        log('Waiting for master removal.')
        assert_no_unit_errors(model)
    await asyncify(_juju_wait)()

    # Try and restore the cluster state
    # Tests following this were passing, but they actually
    # would fail in a multi-master situation
    await workers.add_unit(1)
    await masters.add_unit(1)
    await asyncify(_juju_wait)()
    assert_no_unit_errors(model)


@log_calls_async
async def validate_gpu_support(model):
    ''' Test gpu support. Should be disabled if hardware
    is not detected and functional if hardware is fine'''

    # See if the workers have nvidia
    workers = model.applications['kubernetes-worker']
    action = await workers.units[0].run('lspci -nnk')
    nvidia = True if action.results['Stdout'].lower().count("nvidia") > 0 else False

    # See what the runtime is set to
    config = await workers.get_config()
    runtime = config['docker_runtime']['value']

    master_unit = model.applications['kubernetes-master'].units[0]
    if not nvidia or runtime == 'apt' or runtime == 'upstream':
        # nvidia should not be running
        await retry_async_with_timeout(verify_deleted,
                                       (master_unit, 'ds', 'nvidia-device-plugin-daemonset', '-n kube-system'),
                                       timeout_msg="nvidia-device-plugin-daemonset is setup without nvidia hardware")
    else:
        # nvidia should be running
        await retry_async_with_timeout(verify_ready,
                                       (master_unit, 'ds', ['nvidia-device-plugin-daemonset'], '-n kube-system'),
                                       timeout_msg="nvidia-device-plugin-daemonset not running")

        # Do an addition on the GPU just be sure.
        # First clean any previous runs
        here = os.path.dirname(os.path.abspath(__file__))
        await scp_to(os.path.join(here, "templates", "cuda-add.yaml"), master_unit, "cuda-add.yaml")
        await master_unit.run(
            '/snap/bin/kubectl delete -f /home/ubuntu/cuda-add.yaml')
        await retry_async_with_timeout(verify_deleted,
                                       (master_unit, 'po', 'cuda-vector-add', '-n default'),
                                       timeout_msg="Cleaning of cuda-vector-add pod failed")
        # Run the cuda addition
        cmd = await master_unit.run('/snap/bin/kubectl create -f /home/ubuntu/cuda-add.yaml')
        if not cmd.results['Code'] == '0':
            log('Failed to create cuda-add pod test!')
            log(cmd.results)
            assert False

        async def cuda_test(master):
            action = await master.run('/snap/bin/kubectl log cuda-vector-add')
            log(action.results['Stdout'])
            return action.results['Stdout'].count("Test PASSED") > 0

        await retry_async_with_timeout(cuda_test, (master_unit,),
                                       timeout_msg="Cuda test did not pass",
                                       timeout_insec=1200)


@log_calls_async
async def validate_extra_args(model):
    async def get_filtered_service_args(app, service):
        results = []

        for unit in app.units:
            while True:
                action = await unit.run('pgrep -a ' + service)
                assert action.status == 'completed'

                if action.data['results']['Code'] == '0':
                    raw_output = action.data['results']['Stdout']
                    arg_string = raw_output.partition(' ')[2].partition(' ')[2]
                    args = {arg.strip() for arg in arg_string.split('--')[1:]}
                    results.append(args)
                    break

                await asyncio.sleep(1)

        # charms sometimes choose the master randomly, filter out the master
        # arg so we can do comparisons reliably
        results = [
            {arg for arg in args if not arg.startswith('master=')}
            for args in results
        ]

        return results

    @log_calls_async
    async def run_extra_args_test(app_name, new_config, expected_args):
        app = model.applications[app_name]
        original_config = await app.get_config()
        original_args = {}
        for service in expected_args:
            original_args[service] = await get_filtered_service_args(app, service)

        await app.set_config(new_config)
        await asyncify(_juju_wait)()

        with timeout_for_current_task(600):
            try:
                for service, expected_service_args in expected_args.items():
                    while True:
                        args_per_unit = await get_filtered_service_args(app, service)
                        if all(expected_service_args <= args for args in args_per_unit):
                            break
                        await asyncio.sleep(3)
            except asyncio.CancelledError:
                log('Dumping locals:\n' + pformat(locals()))
                raise

        filtered_original_config = {
            key: original_config[key]['value']
            for key in new_config
        }
        await app.set_config(filtered_original_config)
        await asyncify(_juju_wait)()

        with timeout_for_current_task(600):
            try:
                for service, original_service_args in original_args.items():
                    while True:
                        new_args = await get_filtered_service_args(app, service)
                        if new_args == original_service_args:
                            break
                        await asyncio.sleep(3)
            except asyncio.CancelledError:
                log('Dumping locals:\n' + pformat(locals()))
                raise

    master_task = run_extra_args_test(
        app_name='kubernetes-master',
        new_config={
            'api-extra-args': ' '.join([
                'min-request-timeout=314',  # int arg, overrides a charm default
                'watch-cache',              # bool arg, implied true
                'enable-swagger-ui=false'   # bool arg, explicit false
            ]),
            'controller-manager-extra-args': ' '.join([
                'v=3',                        # int arg, overrides a charm default
                'profiling',                  # bool arg, implied true
                'contention-profiling=false'  # bool arg, explicit false
            ]),
            'scheduler-extra-args': ' '.join([
                'v=3',                        # int arg, overrides a charm default
                'profiling',                  # bool arg, implied true
                'contention-profiling=false'  # bool arg, explicit false
            ])
        },
        expected_args={
            'kube-apiserver': {
                'min-request-timeout=314',
                'watch-cache',
                'enable-swagger-ui=false'
            },
            'kube-controller': {
                'v=3',
                'profiling',
                'contention-profiling=false'
            },
            'kube-scheduler': {
                'v=3',
                'profiling',
                'contention-profiling=false'
            }
        }
    )

    worker_task = run_extra_args_test(
        app_name='kubernetes-worker',
        new_config={
            'kubelet-extra-args': ' '.join([
                'v=1',                   # int arg, overrides a charm default
                'enable-server',         # bool arg, implied true
                'alsologtostderr=false'  # bool arg, explicit false
            ]),
            'proxy-extra-args': ' '.join([
                'v=1',                   # int arg, overrides a charm default
                'profiling',             # bool arg, implied true
                'alsologtostderr=false'  # bool arg, explicit false
            ])
        },
        expected_args={
            'kubelet': {
                'v=1',
                'enable-server',
                'alsologtostderr=false'
            },
            'kube-proxy': {
                'v=1',
                'profiling',
                'alsologtostderr=false'
            }
        }
    )

    await asyncio.gather(master_task, worker_task)


@log_calls_async
async def validate_kubelet_extra_config(model):
    worker_app = model.applications['kubernetes-worker']
    k8s_version_str = worker_app.data['workload-version']
    k8s_minor_version = tuple(int(i) for i in k8s_version_str.split('.')[:2])
    if k8s_minor_version < (1, 10):
        log('skipping, k8s version v' + k8s_version_str)
        return

    config = await worker_app.get_config()
    old_extra_config = config['kubelet-extra-config']['value']

    # set the new config
    new_extra_config = yaml.dump({
        # maxPods, because it can be observed in the Node object
        'maxPods': 111,
        # evictionHard/memory.available, because it has a nested element
        'evictionHard': {
            'memory.available': '200Mi'
        },
        # authentication/webhook/enabled, so we can confirm that other
        # items in the authentication section are preserved
        'authentication': {
            'webhook': {
                'enabled': False
            }
        }
    })
    await set_config_and_wait(worker_app, {'kubelet-extra-config': new_extra_config})

    # wait for and validate new maxPods value
    log('waiting for nodes to show new pod capacity')
    master_unit = model.applications['kubernetes-master'].units[0]
    while True:
        cmd = '/snap/bin/kubectl -o yaml get node'
        action = await master_unit.run(str(cmd))
        if action.status == 'completed' and action.results['Code'] == '0':
            nodes = yaml.load(action.results['Stdout'])

            all_nodes_updated = all([
                node['status']['capacity']['pods'] == '111'
                for node in nodes['items']
            ])
            if all_nodes_updated:
                break

        await asyncio.sleep(1)

    # validate config.yaml on each worker
    log('validating generated config.yaml files')
    for worker_unit in worker_app.units:
        cmd = 'cat /root/cdk/kubelet/config.yaml'
        action = await worker_unit.run(cmd)
        if action.status == 'completed' and action.results['Code'] == '0':
            config = yaml.load(action.results['Stdout'])
            assert config['evictionHard']['memory.available'] == '200Mi'
            assert config['authentication']['webhook']['enabled'] == False
            assert 'anonymous' in config['authentication']
            assert 'x509' in config['authentication']

    # clean up
    await set_config_and_wait(
        worker_app,
        {'kubelet-extra-config': old_extra_config})


@log_calls_async
async def validate_sans(model):
    example_domain = "santest.example.com"
    app = model.applications['kubernetes-master']
    original_config = await app.get_config()
    lb = None
    original_lb_config = None
    if 'kubeapi-load-balancer' in model.applications:
        lb = model.applications['kubeapi-load-balancer']
        original_lb_config = await lb.get_config()

    async def get_server_certs():
        results = []
        for unit in app.units:
            action = await unit.run('openssl s_client -connect 127.0.0.1:6443 </dev/null 2>/dev/null | openssl x509 -text')
            assert action.status == 'completed'
            raw_output = action.data['results']['Stdout']
            results.append(raw_output)

        # if there is a load balancer, ask it as well
        if lb is not None:
            for unit in lb.units:
                action = await unit.run('openssl s_client -connect 127.0.0.1:443 </dev/null 2>/dev/null | openssl x509 -text')
                assert action.status == 'completed'
                raw_output = action.data['results']['Stdout']
                results.append(raw_output)

        return results

    async def all_certs_removed():
        certs = await get_server_certs()
        if any(example_domain in cert for cert in certs):
            return False
        return True

    async def all_certs_in_place():
        certs = await get_server_certs()
        if not all(example_domain in cert for cert in certs):
            return False
        return True

    # add san to extra san list
    await app.set_config({'extra_sans': example_domain})
    if lb is not None:
        await lb.set_config({'extra_sans': example_domain})

    # wait for server certs to update
    await retry_async_with_timeout(all_certs_in_place, (),
                                   timeout_msg='extra sans config did not propagate to server certs')

    # now remove it
    await app.set_config({'extra_sans': ''})
    if lb is not None:
        await lb.set_config({'extra_sans': ''})

    # verify it went away
    await retry_async_with_timeout(all_certs_removed, (),
                                   timeout_msg='extra sans config did not propagate to server certs')

    # reset back to what they had before
    await app.set_config({'extra_sans': original_config['extra_sans']['value']})
    if lb is not None and original_lb_config is not None:
        await lb.set_config({'extra_sans': original_lb_config['extra_sans']['value']})


@log_calls_async
async def run_until_success(unit, cmd, timeout_insec=None):
    while True:
        action = await unit.run(cmd, timeout=timeout_insec)
        if (action.status == 'completed' and
                'results' in action.data and
                action.data['results']['Code'] == '0'):
            return action.data['results']['Stdout']
        else:
            log('Action ' + action.status + '. Command failed on unit ' + unit.entity_id)
            log('cmd: ' + cmd)
            if 'results' in action.data:
                log('code: ' + action.data['results']['Code'])
                log('stdout:\n' + action.data['results']['Stdout'].strip())
                log('stderr:\n' + action.data['results']['Stderr'].strip())
                log('Will retry...')
            await asyncio.sleep(5)


@log_calls_async
async def validate_docker_logins(model):
    # Choose a worker. He shall be our vessel.
    app = model.applications['kubernetes-worker']
    vessel = app.units[0]

    async def kubectl(cmd):
        cmd = '/snap/bin/kubectl --kubeconfig /root/cdk/kubeconfig ' + cmd
        return await run_until_success(vessel, cmd)

    @log_calls_async
    async def wait_for_test_pod_state(desired_state, desired_reason=None):
        while True:
            data = await kubectl_get('po test-registry-user')
            status = data['status']
            if 'containerStatuses' in status:
                container_status = status['containerStatuses'][0]
                state, details = list(container_status['state'].items())[0]
                if desired_reason:
                    reason = details.get('reason')
                    if state == desired_state and reason == desired_reason:
                        break
                elif state == desired_state:
                    break
            await asyncio.sleep(1)

    @log_calls_async
    async def kubectl_delete(target):
        cmd = 'delete --ignore-not-found ' + target
        return await kubectl(cmd)

    @log_calls_async
    async def cleanup():
        await app.set_config({'docker-logins': '[]'})
        await kubectl_delete('svc test-registry')
        await kubectl_delete('po test-registry-user')
        await kubectl_delete('po test-registry')
        # wait for the pods to clear before removing the mounted secrets
        log('Waiting for pods to finish terminating...')
        while True:
            output = await kubectl('get po')
            if 'test-registry' not in output:
                break
            await asyncio.sleep(1)
        await kubectl_delete('secret test-registry')
        cmd = 'rm -rf /tmp/test-registry'
        await run_until_success(vessel, cmd)

    @log_calls_async
    async def kubectl_get(target):
        cmd = 'get -o json ' + target
        output = await kubectl(cmd)
        return json.loads(output)

    @log_calls_async
    async def kubectl_create(definition):
        with NamedTemporaryFile('w') as f:
            json.dump(definition, f)
            f.flush()
            await scp_to(f.name, vessel, '/tmp/test-registry/temp.yaml')
        await kubectl('create -f /tmp/test-registry/temp.yaml')

    # Start with a clean environment
    await cleanup()
    await run_until_success(vessel, 'mkdir -p /tmp/test-registry')
    await run_until_success(vessel, 'chown ubuntu:ubuntu /tmp/test-registry')

    # Create registry secret
    here = os.path.dirname(os.path.abspath(__file__))
    htpasswd = os.path.join(here, 'templates', 'test-registry', 'htpasswd')
    await scp_to(htpasswd, vessel, '/tmp/test-registry')
    cmd = 'openssl req -x509 -newkey rsa:4096 -keyout /tmp/test-registry/tls.key -out /tmp/test-registry/tls.crt -days 2 -nodes -subj /CN=localhost'
    await run_until_success(vessel, cmd)
    await kubectl('create secret generic test-registry'
        + ' --from-file=/tmp/test-registry/htpasswd'
        + ' --from-file=/tmp/test-registry/tls.crt'
        + ' --from-file=/tmp/test-registry/tls.key'
    )

    # Create registry
    await kubectl_create({
        'apiVersion': 'v1',
        'kind': 'Pod',
        'metadata': {
            'name': 'test-registry',
            'labels': {
                'app': 'test-registry'
            }
        },
        'spec': {
            'containers': [{
                'name': 'registry',
                'image': 'registry:2.6.2',
                'ports': [{
                    'containerPort': 5000,
                    'protocol': 'TCP'
                }],
                'env': [
                    {'name': 'REGISTRY_AUTH_HTPASSWD_REALM', 'value': 'test-registry'},
                    {'name': 'REGISTRY_AUTH_HTPASSWD_PATH', 'value': '/secret/htpasswd'},
                    {'name': 'REGISTRY_HTTP_TLS_KEY', 'value': '/secret/tls.key'},
                    {'name': 'REGISTRY_HTTP_TLS_CERTIFICATE', 'value': '/secret/tls.crt'}
                ],
                'volumeMounts': [
                    {
                        'name': 'secret',
                        'mountPath': '/secret'
                    },
                    {
                        'name': 'data',
                        'mountPath': '/var/lib/registry'
                    }
                ]
            }],
            'volumes': [
                {
                    'name': 'secret',
                    'secret': {
                        'secretName': 'test-registry'
                    }
                },
                {
                    'name': 'data',
                    'emptyDir': {}
                }
            ]
        }
    })
    await kubectl_create({
        'apiVersion': 'v1',
        'kind': 'Service',
        'metadata': {
            'name': 'test-registry'
        },
        'spec': {
            'type': 'NodePort',
            'selector': {
                'app': 'test-registry'
            },
            'ports': [{
                'protocol': 'TCP',
                'port': 5000,
                'targetPort': 5000
            }]
        }
    })
    registry_service = await kubectl_get('service test-registry')
    registry_port = registry_service['spec']['ports'][0]['nodePort']
    registry_url = 'localhost:%s' % registry_port

    log('Waiting for service to come up...')
    while True:
        svc_output = await kubectl('get svc')
        po_output = await kubectl('get po')
        if 'test-registry' in po_output and 'test-registry' in svc_output:
            break
        await asyncio.sleep(10)
    log('Start testing...')

    # Upload test container
    cmd = 'docker login %s -u test-user -p yyDVinHE' % registry_url
    await run_until_success(vessel, cmd, timeout_insec=60)
    cmd = 'docker pull ubuntu:16.04'
    await run_until_success(vessel, cmd)
    cmd = 'docker tag ubuntu:16.04 %s/ubuntu:16.04' % registry_url
    await run_until_success(vessel, cmd)
    cmd = 'docker push %s/ubuntu:16.04' % registry_url
    await run_until_success(vessel, cmd)
    cmd = 'docker rmi %s/ubuntu:16.04' % registry_url
    await run_until_success(vessel, cmd)
    cmd = 'docker logout %s' % registry_url
    await run_until_success(vessel, cmd, timeout_insec=60)

    # Create test pod using our registry
    await kubectl_create({
        'apiVersion': 'v1',
        'kind': 'Pod',
        'metadata': {
            'name': 'test-registry-user'
        },
        'spec': {
            'containers': [{
                'name': 'ubuntu',
                'image': registry_url + '/ubuntu:16.04',
                'command': ['sleep', '3600']
            }]
        }
    })

    # Verify pod fails image pull
    await wait_for_test_pod_state('waiting', 'ImagePullBackOff')

    # Configure docker_logins
    docker_logins = [{'server': registry_url, 'username': 'test-user', 'password': 'yyDVinHE'}]
    await app.set_config({'docker-logins': json.dumps(docker_logins)})

    # Verify pod enters running state
    await wait_for_test_pod_state('running')

    # Restore config and clean up
    await cleanup()


@log_calls_async
async def get_last_audit_entry_date(unit):
    cmd = 'cat /root/cdk/audit/audit.log | tail -n 1'
    raw = await run_until_success(unit, cmd)
    data = json.loads(raw)
    if 'timestamp' in data:
        timestamp = data['timestamp']
        time = datetime.strptime(timestamp, '%Y-%m-%dT%H:%M:%SZ')
    elif 'requestReceivedTimestamp' in data:
        timestamp = data['requestReceivedTimestamp']
        time = datetime.strptime(timestamp, '%Y-%m-%dT%H:%M:%S.%fZ')
    else:
        raise AuditTimestampError('Unable to find timestamp in {}'.format(data))

    return time


@async_contextmanager
async def assert_hook_occurs_on_all_units(app, hook):
    started_units = set()
    finished_units = set()

    for unit in app.units:
        @unit.on_change
        async def on_change(delta, old, new, model):
            unit_id = new.entity_id
            if new.agent_status_message == 'running ' + hook + ' hook':
                started_units.add(unit_id)
            if new.agent_status == 'idle' and unit_id in started_units:
                finished_units.add(unit_id)

    await yield_()

    log('assert_hook_occurs_on_all_units: waiting for ' + hook + ' hook')
    while len(finished_units) < len(app.units):
        await asyncio.sleep(1)


@log_calls_async
async def set_config_and_wait(app, config):
    current_config = await app.get_config()

    if all(config[key] == current_config[key]['value'] for key in config):
        log('set_config_and_wait: new config identical to current, skipping')
        return

    async with assert_hook_occurs_on_all_units(app, 'config-changed'):
        await app.set_config(config)
        await asyncify(_juju_wait)()


@log_calls_async
async def reset_audit_config(master_app):
    config = await master_app.get_config()
    await set_config_and_wait(master_app, {
        'audit-policy': config['audit-policy']['default'],
        'audit-webhook-config': config['audit-webhook-config']['default'],
        'api-extra-args': config['api-extra-args']['default']
    })


@log_calls_async
async def validate_audit_default_config(model):
    app = model.applications['kubernetes-master']

    # Ensure we're using default configuration
    await reset_audit_config(app)

    # Verify new entries are being logged
    unit = app.units[0]
    before_date = await get_last_audit_entry_date(unit)
    await asyncio.sleep(1)
    await run_until_success(unit, '/snap/bin/kubectl get po')
    after_date = await get_last_audit_entry_date(unit)
    assert after_date > before_date

    # Verify total log size is less than 1 GB
    raw = await run_until_success(unit, 'du -bs /root/cdk/audit')
    size_in_bytes = int(raw.split()[0])
    log("Audit log size in bytes: %d" % size_in_bytes)
    max_size_in_bytes = 1000 * 1000 * 1000 * 1.01  # 1 GB, plus some tolerance
    assert size_in_bytes <= max_size_in_bytes

    # Clean up
    await reset_audit_config(app)


@log_calls_async
async def validate_audit_empty_policy(model):
    app = model.applications['kubernetes-master']

    # Set audit-policy to blank
    await reset_audit_config(app)
    await set_config_and_wait(app, {'audit-policy': ''})

    # Verify no entries are being logged
    unit = app.units[0]
    before_date = await get_last_audit_entry_date(unit)
    await asyncio.sleep(1)
    await run_until_success(unit, '/snap/bin/kubectl get po')
    after_date = await get_last_audit_entry_date(unit)
    assert after_date == before_date

    # Clean up
    await reset_audit_config(app)


@log_calls_async
async def validate_audit_custom_policy(model):
    app = model.applications['kubernetes-master']

    # Set a custom policy that only logs requests to a special namespace
    namespace = 'validate-audit-custom-policy'
    policy = {
        'apiVersion': 'audit.k8s.io/v1beta1',
        'kind': 'Policy',
        'rules': [
            {
                'level': 'Metadata',
                'namespaces': [namespace]
            },
            {'level': 'None'}
        ]
    }
    await reset_audit_config(app)
    await set_config_and_wait(app, {'audit-policy': yaml.dump(policy)})

    # Verify no entries are being logged
    unit = app.units[0]
    before_date = await get_last_audit_entry_date(unit)
    await asyncio.sleep(1)
    await run_until_success(unit, '/snap/bin/kubectl get po')
    after_date = await get_last_audit_entry_date(unit)
    assert after_date == before_date

    # Create our special namespace
    namespace_definition = {
        'apiVersion': 'v1',
        'kind': 'Namespace',
        'metadata': {
            'name': namespace
        }
    }
    path = '/tmp/validate_audit_custom_policy-namespace.yaml'
    with NamedTemporaryFile('w') as f:
        json.dump(namespace_definition, f)
        f.flush()
        await scp_to(f.name, unit, path)
    await run_until_success(unit, '/snap/bin/kubectl create -f ' + path)

    # Verify our very special request gets logged
    before_date = await get_last_audit_entry_date(unit)
    await asyncio.sleep(1)
    await run_until_success(unit, '/snap/bin/kubectl get po -n ' + namespace)
    after_date = await get_last_audit_entry_date(unit)
    assert after_date > before_date

    # Clean up
    await run_until_success(unit, '/snap/bin/kubectl delete ns ' + namespace)
    await reset_audit_config(app)


@log_calls_async
async def validate_audit_webhook(model):
    app = model.applications['kubernetes-master']
    unit = app.units[0]

    async def get_webhook_server_entry_count():
        cmd = '/snap/bin/kubectl logs test-audit-webhook'
        raw = await run_until_success(unit, cmd)
        lines = raw.splitlines()
        count = len(lines)
        return count

    # Deploy an nginx target for webhook
    cmd = '/snap/bin/kubectl delete --ignore-not-found po test-audit-webhook'
    await run_until_success(unit, cmd)
    cmd = '/snap/bin/kubectl run test-audit-webhook --image nginx:1.15.0-alpine --restart Never'
    await run_until_success(unit, cmd)
    nginx_ip = None
    while nginx_ip is None:
        cmd = '/snap/bin/kubectl get po -o json test-audit-webhook'
        raw = await run_until_success(unit, cmd)
        pod = json.loads(raw)
        nginx_ip = pod['status'].get('podIP', None)

    # Set audit config with webhook enabled
    audit_webhook_config = {
        'apiVersion': 'v1',
        'kind': 'Config',
        'clusters': [{
            'name': 'test-audit-webhook',
            'cluster': {
                'server': 'http://' + nginx_ip
            }
        }],
        'contexts': [{
            'name': 'test-audit-webhook',
            'context': {
                'cluster': 'test-audit-webhook'
            }
        }],
        'current-context': 'test-audit-webhook'
    }
    await reset_audit_config(app)
    await set_config_and_wait(app, {
        'audit-webhook-config': yaml.dump(audit_webhook_config),
        'api-extra-args': 'audit-webhook-mode=blocking'
    })

    # Ensure webhook log is growing
    before_count = await get_webhook_server_entry_count()
    await run_until_success(unit, '/snap/bin/kubectl get po')
    after_count = await get_webhook_server_entry_count()
    assert after_count > before_count

    # Clean up
    await reset_audit_config(app)


@log_calls_async
async def validate_keystone(model):
    masters = model.applications['kubernetes-master']
    k8s_version_str = masters.data['workload-version']
    k8s_minor_version = tuple(int(i) for i in k8s_version_str.split('.')[:2])
    if k8s_minor_version < (1, 12):
        log('skipping, k8s version v' + k8s_version_str)
        return

    # save off config
    config = await model.applications['kubernetes-master'].get_config()

    # add keystone
    await model.deploy('keystone', series='bionic',
                       config={'admin-password': 'testpw',
                               'preferred-api-version': '3',
                               'openstack-origin': 'cloud:bionic-rocky'})
    await model.deploy('percona-cluster',
                       config={'innodb-buffer-pool-size': '256M',
                               'max-connections': '1000'})
    await model.add_relation('kubernetes-master:keystone-credentials',
                             'keystone:identity-credentials')
    await model.add_relation('keystone:shared-db', 'percona-cluster:shared-db')
    await asyncify(_juju_wait)()

    # verify kubectl config file has keystone in it
    one_master = random.choice(masters.units)
    for i in range(5):
        action = await one_master.run('cat /home/ubuntu/config')
        if 'client-keystone-auth' in action.results['Stdout']:
            break
        log("Unable to find keystone information in kubeconfig, retrying...")
        await asyncio.sleep(10)

    assert 'client-keystone-auth' in action.results['Stdout']

    # verify kube-keystone.sh exists
    one_master = random.choice(masters.units)
    action = await one_master.run('cat /home/ubuntu/kube-keystone.sh')
    assert 'OS_AUTH_URL' in action.results['Stdout']

    # verify webhook enabled on apiserver
    await wait_for_process(model, 'authentication-token-webhook-config-file')
    one_master = random.choice(masters.units)
    action = await one_master.run('sudo cat /root/cdk/keystone/webhook.yaml')
    assert 'webhook' in action.results['Stdout']

    # verify keystone pod is running
    await retry_async_with_timeout(verify_ready,
                                   (one_master, 'po', ['k8s-keystone-auth'], '-n kube-system'),
                                   timeout_msg="Unable to find keystone auth pod before timeout")

    skip_tests = False
    action = await one_master.run('cat /snap/cdk-addons/current/templates/keystone-rbac.yaml')
    if 'kind: Role' in action.results['Stdout']:
        # we need to skip tests for the old template that incorrectly had a Role instead
        # of a ClusterRole
        skip_tests = True

    if skip_tests:
        await masters.set_config({'enable-keystone-authorization': 'true'})
    else:
        # verify authorization
        await masters.set_config({'enable-keystone-authorization': 'true',
                                  'authorization-mode': 'Node,Webhook,RBAC'})
    await wait_for_process(model, 'authorization-webhook-config-file')

    # verify auth fail - bad user
    one_master = random.choice(masters.units)
    await one_master.run('/usr/bin/snap install --edge client-keystone-auth')

    cmd = "source /home/ubuntu/kube-keystone.sh && \
           OS_PROJECT_NAME=k8s OS_DOMAIN_NAME=k8s OS_USERNAME=fake \
           OS_PASSWORD=bad /snap/bin/kubectl --kubeconfig /home/ubuntu/config get clusterroles"
    output = await one_master.run(cmd)
    assert output.status == 'completed'
    assert "invalid user credentials" in output.data['results']['Stderr'].lower()

    # verify auth fail - bad password
    cmd = "source /home/ubuntu/kube-keystone.sh && \
           OS_PROJECT_NAME=admin OS_DOMAIN_NAME=admin_domain OS_USERNAME=admin \
           OS_PASSWORD=badpw /snap/bin/kubectl --kubeconfig /home/ubuntu/config get clusterroles"
    output = await one_master.run(cmd)
    assert output.status == 'completed'
    assert "invalid user credentials" in output.data['results']['Stderr'].lower()

    if not skip_tests:
        # set up read only access to pods only
        await masters.set_config({'keystone-policy': '''apiVersion: v1
kind: ConfigMap
metadata:
  name: k8s-auth-policy
  namespace: kube-system
  labels:
    k8s-app: k8s-keystone-auth
data:
  policies: |
    [
      {
        "resource": {
          "verbs": ["get", "list", "watch"],
          "resources": ["pods"],
          "version": "*",
          "namespace": "default"
        },
        "match": [
          {
            "type": "user",
            "values": ["admin"]
          }
        ]
      }
    ]'''})
        await asyncify(_juju_wait)()

        # verify auth failure on something not a pod
        cmd = "source /home/ubuntu/kube-keystone.sh && \
            OS_PROJECT_NAME=admin OS_DOMAIN_NAME=admin_domain OS_USERNAME=admin \
            OS_PASSWORD=testpw /snap/bin/kubectl \
            --kubeconfig /home/ubuntu/config get clusterroles"
        output = await one_master.run(cmd)
        assert output.status == 'completed'
        assert "error" in output.data['results']['Stderr'].lower()

        # the config set writes out a file and updates a configmap, which is then picked up by the
        # keystone pod and updated. This all takes time and I don't know of a great way to tell
        # that it is all done. I could compare the configmap to this, but that doesn't mean the
        # pod has updated. The pod does write a log line about the configmap updating, but
        # I'd need to watch both in succession and it just seems much easier and just as reliable
        # to just retry on failure a few times.

        for i in range(18):  # 3 minutes
            # verify auth success on pods
            cmd = "source /home/ubuntu/kube-keystone.sh && \
                OS_PROJECT_NAME=admin OS_DOMAIN_NAME=admin_domain OS_USERNAME=admin \
                OS_PASSWORD=testpw /snap/bin/kubectl \
                --kubeconfig /home/ubuntu/config get po"
            output = await one_master.run(cmd)
            if (output.status == 'completed' and
                    "invalid user credentials" not in output.data['results']['Stderr'].lower() and
                    "error" not in output.data['results']['Stderr'].lower()):
                break
            log("Unable to verify configmap change, retrying...")
            await asyncio.sleep(10)

        assert output.status == 'completed'
        assert "invalid user credentials" not in output.data['results']['Stderr'].lower()
        assert "error" not in output.data['results']['Stderr'].lower()

        # verify auth failure on pods outside of default namespace
        cmd = "source /home/ubuntu/kube-keystone.sh && \
            OS_PROJECT_NAME=admin OS_DOMAIN_NAME=admin_domain OS_USERNAME=admin \
            OS_PASSWORD=testpw /snap/bin/kubectl \
            --kubeconfig /home/ubuntu/config get po -n kube-system"
        output = await one_master.run(cmd)
        assert output.status == 'completed'
        assert "invalid user credentials" not in output.data['results']['Stderr'].lower()
        assert "forbidden" in output.data['results']['Stderr'].lower()

    # verify auth works now that it is off
    original_auth = config['authorization-mode']['value']
    await masters.set_config({'enable-keystone-authorization': 'false',
                              'authorization-mode': original_auth})
    await wait_for_not_process(model, 'authorization-webhook-config-file')
    await asyncify(_juju_wait)()
    cmd = "/snap/bin/kubectl --context=juju-context \
           --kubeconfig /home/ubuntu/config get clusterroles"
    output = await one_master.run(cmd)
    assert output.status == 'completed'
    assert "invalid user credentials" not in output.data['results']['Stderr'].lower()
    assert "error" not in output.data['results']['Stderr'].lower()
    assert "forbidden" not in output.data['results']['Stderr'].lower()

    # cleanup
    await model.applications['keystone'].destroy()
    await model.applications['percona-cluster'].destroy()


@log_calls_async
async def validate_encryption_at_rest(model):
    try:
        # setup
        await model.deploy('cs:~openstack-charmers-next/vault',
                           config={'auto-generate-root-ca-cert': True,
                                   'totally-unsecure-auto-unlock': True})
        await model.deploy('percona-cluster')
        await model.add_relation('vault:shared-db',
                                 'percona-cluster:shared-db')
        await model.remove_relation('easyrsa:client',
                                    'kubernetes-master:certificates')
        await model.remove_relation('easyrsa:client',
                                    'kubernetes-worker:certificates')
        await model.add_relation('vault:certificates',
                                 'kubernetes-master:certificates')
        await model.add_relation('vault:certificates',
                                 'kubernetes-worker:certificates')
        await model.add_relation('kubernetes-master:vault-kv',
                                 'vault:secrets')
        await asyncify(_juju_wait)()
        # create secret
        worker = model.applications['kubernetes-worker'].units[0]
        output = await worker.run("kubectl create secret generic test-secret "
                                  "--from-literal=username='secret-value'")
        assert output.status == 'completed'
        # read secret
        output = await worker.run("kubectl get secret test-secret -o yaml")
        assert output.status == 'completed'
        assert 'secret-value' in output.results['Stdout']
        # verify secret is encrypted
        etcd = model.applications['etcd'].units[0]
        etcd.run("ETCDCTL_API=3 /snap/bin/etcd.etcdctl "
                 "--endpoints http://127.0.0.1:4001 "
                 "get /registry/secrets/default/test-secret | hexdump -C")
        assert output.status == 'completed'
        assert 'secret-value' not in output.results['Stdout']
    finally:
        # cleanup
        (done1, pending1) = await asyncio.wait({
            model.applications['percona-cluster'].destroy(),
            model.applications['vault'].destroy(),
        })
        # wait for vault to go away so we don't have 2 cert providers at once
        await asyncify(_juju_wait)()
        (done2, pending2) = await asyncio.wait({
            model.add_relation('easyrsa:client',
                               'kubernetes-master:certificates'),
            model.add_relation('easyrsa:client',
                               'kubernetes-worker:certificates'),
        })
        await asyncify(_juju_wait)()
        for task in done1 + done2:
            # read and ignore any exception so that it doesn't get raised
            # when the task is GC'd
            task.exception()


class MicrobotError(Exception):
    pass


class AuditTimestampError(Exception):
    pass
