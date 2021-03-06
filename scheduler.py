import subprocess
from abc import ABCMeta, abstractmethod
from sklearn.cross_validation import LeaveOneOut
from cluster import Cluster, Node
from application import Application
from complementarity import ComplementarityEstimation
from job_group_data import JobGroupData
from repeated_timer import RepeatedTimer
from threading import Lock
from typing import List
import time
import numpy as np


class NoApplicationCanBeScheduled(BaseException):
    pass


class Scheduler(metaclass=ABCMeta):

    jobs_to_peek_arg = 7
    activate_random_arrival = False
    waiting_limit = -1

    def __init__(self, estimation: ComplementarityEstimation, cluster: Cluster, update_interval=60):
        self.queue = []
        self.estimation = estimation
        self.cluster = cluster
        self._timer = RepeatedTimer(update_interval, self.update_estimation)
        self.scheduler_lock = Lock()
        self.started_at = None
        self.stopped_at = None
        self.print_estimation = False
        self.waiting_time = {}
        self.scheduled_apps_num = 0
        self.jobs_to_peek = self.jobs_to_peek_arg
        self.random_arrival_rate = [0, 0, 0, 0, 0, 1, 2, 0, 0, 0, 1, 0, 2, 0, 2,
                                    1, 0, 2, 2, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0]

    def start(self):
        self.schedule()
        self._timer.start()
        self.started_at = time.time() - 3600

    def stop(self):
        self._timer.cancel()
        self.stopped_at = time.time() - 3600

    def update_estimation(self):
        for (apps, usage) in self.cluster.apps_usage():
            if len(apps) > 0 and usage.is_not_idle():
                for rest, out in LeaveOneOut(len(apps)):
                    self.estimation.update_app(apps[out[0]], [apps[i] for i in rest], usage.rate())
        if self.print_estimation:
            self.estimation.print()

    def add(self, app: Application):
        self.queue.append(app)

    def add_all(self, apps: List[Application]):
        self.queue.extend(apps)

    def schedule(self):
        while len(self.queue) > 0:
            try:
                app = self.schedule_application()
                if app.waiting_time != 0:
                    app.waiting_time = app.waiting_time - 1
                if app.waiting_time in self.waiting_time.keys():
                    self.waiting_time[app.waiting_time] = self.waiting_time[app.waiting_time] + 1
                else:
                    self.waiting_time[app.waiting_time] = 1
            except NoApplicationCanBeScheduled:
                print("No Application can be scheduled right now")
                break
            app.start(self.cluster.resource_manager, self._on_app_finished)
            if self.jobs_to_peek < len(self.queue) and self.activate_random_arrival:
                print("Update random arrival rate")
                self.jobs_to_peek = self.jobs_to_peek + self.random_arrival_rate[self.scheduled_apps_num]
            print("Scheduler round: {}".format(self.scheduled_apps_num))
            print("Jobs_to_peek = {}".format(self.jobs_to_peek))
            self.scheduled_apps_num = self.scheduled_apps_num + 1
            time.sleep(1) # add a slight delay so jobs could be submitted to yarn in order
        self.cluster.print_nodes()

    def schedule_application(self) -> Application:
        if self.cluster.available_containers()==0:
            raise NoApplicationCanBeScheduled
        app = self.get_application_to_schedule()
        if app.n_containers > self.cluster.available_containers():
            self.queue = [app] + self.queue
            raise NoApplicationCanBeScheduled

        self.place_containers(app)

        return app

    def _on_app_finished(self, app: Application):
        self.scheduler_lock.acquire()
        self.cluster.remove_applications(app)
        if len(self.queue) == 0 and self.cluster.has_application_scheduled() == 0:
            self.stop()
            self.on_stop()
        else:
            self.schedule()
        self.scheduler_lock.release()

    def on_stop(self):
        delta = self.stopped_at - self.started_at
        print("Queue took {:.0f}'{:.0f} to complete".format(delta // 60, delta % 60))
        self.estimation.save(self.estimation.output_folder)
        self.export_experiment_data()
        print("\n\n\n((((((((((  Waiting times  ))))))))))")
        for (key, value) in self.waiting_time.items():
            print("{} rounds waiting - {}".format(key,value))
        print(str(self.waiting_time))

    def export_experiment_data(self):
        print("\n\n\n=======Generate experiment output=======\n\n\n")
        host_list = "|".join([address for address in self.cluster.nodes.keys()])

        cmd_query_cpu = "\ninflux -precision rfc3339 -username root -password root" \
                        " -database 'telegraf' -host 'localhost' -execute 'SELECT usage_user,usage_iowait " \
                        "FROM \"telegraf\".\"autogen\".\"cpu\" WHERE time > '\\''{}'\\'' and time < '\\''{}'\\'' AND host =~ /{}/  " \
                        "AND cpu = '\\''cpu-total'\\'' GROUP BY host' -format 'csv' > /data/vinh.tran/new/expData/{}/cpu_{}.csv" \
            .format(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime(self.started_at)),
                    time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime(self.stopped_at)),
                    host_list,
                    Application.experiment_name,
                    Application.experiment_name)
        print(cmd_query_cpu)
        # subprocess.Popen(cmd_query_cpu, shell=True)

        cmd_query_cpu_mean = "\ninflux -precision rfc3339 -username root -password root" \
                             " -database 'telegraf' -host 'localhost' -execute 'SELECT mean(usage_user) as \"mean_cpu_percent\",mean(usage_iowait) as \"mean_io_wait\" " \
                             "FROM \"telegraf\".\"autogen\".\"cpu\" WHERE time > '\\''{}'\\'' and time < '\\''{}'\\'' AND host =~ /{}/  " \
                             "AND cpu = '\\''cpu-total'\\'' GROUP BY time(10s)' -format 'csv' > /data/vinh.tran/new/expData/{}/cpu_{}_mean.csv" \
            .format(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime(self.started_at)),
                    time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime(self.stopped_at)),
                    host_list,
                    Application.experiment_name,
                    Application.experiment_name)
        print(cmd_query_cpu_mean)

        cmd_query_mem = "\ninflux -precision rfc3339 -username root -password root " \
                        "-database 'telegraf' -host 'localhost' -execute 'SELECT used_percent " \
                        "FROM \"telegraf\".\"autogen\".\"mem\" WHERE time > '\\''{}'\\'' and time < '\\''{}'\\'' AND host =~ /{}/  " \
                        "GROUP BY host' -format 'csv' > /data/vinh.tran/new/expData/{}/mem_{}.csv" \
            .format(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime(self.started_at)),
                    time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime(self.stopped_at)),
                    host_list,
                    Application.experiment_name,
                    Application.experiment_name)
        print(cmd_query_mem)

        cmd_query_mem_mean = "\ninflux -precision rfc3339 -username root -password root " \
                             "-database 'telegraf' -host 'localhost' -execute 'SELECT mean(used_percent) " \
                             "FROM \"telegraf\".\"autogen\".\"mem\" WHERE time > '\\''{}'\\'' and time < '\\''{}'\\'' AND host =~ /{}/  " \
                             "GROUP BY time(10s)' -format 'csv' > /data/vinh.tran/new/expData/{}/mem_{}_mean.csv" \
            .format(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime(self.started_at)),
                    time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime(self.stopped_at)),
                    host_list,
                    Application.experiment_name,
                    Application.experiment_name)
        print(cmd_query_mem_mean)

        cmd_query_disk = "\ninflux -precision rfc3339 -username root -password root " \
                         "-database 'telegraf' -host 'localhost' -execute 'SELECT sum(read_bytes),sum(write_bytes) " \
                         "FROM (SELECT derivative(last(\"read_bytes\"),1s) as \"read_bytes\",derivative(last(\"write_bytes\"),1s) as \"write_bytes\",derivative(last(\"io_time\"),1s) as \"io_time\" " \
                         "FROM \"telegraf\".\"autogen\".\"diskio\" WHERE time > '\\''{}'\\'' and time < '\\''{}'\\'' AND host =~ /{}/  " \
                         "GROUP BY \"host\",\"name\",time(10s)) WHERE time > '\\''{}'\\'' and time < '\\''{}'\\'' GROUP BY host,time(10s)' -format 'csv' > /data/vinh.tran/new/expData/{}/disk_{}.csv" \
            .format(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime(self.started_at)),
                    time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime(self.stopped_at)),
                    host_list,
                    time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime(self.started_at)),
                    time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime(self.stopped_at)),
                    Application.experiment_name,
                    Application.experiment_name)
        print(cmd_query_disk)

        cmd_query_disk_mean = "\ninflux -precision rfc3339 -username root -password root " \
                              "-database 'telegraf' -host 'localhost' -execute 'SELECT sum(read_bytes),sum(write_bytes) " \
                              "FROM (SELECT derivative(last(\"read_bytes\"),1s) as \"read_bytes\",derivative(last(\"write_bytes\"),1s) as \"write_bytes\",derivative(last(\"io_time\"),1s) as \"io_time\" " \
                              "FROM \"telegraf\".\"autogen\".\"diskio\" WHERE time > '\\''{}'\\'' and time < '\\''{}'\\'' AND host =~ /{}/  " \
                              "GROUP BY \"host\",\"name\",time(10s)) WHERE time > '\\''{}'\\'' and time < '\\''{}'\\'' GROUP BY time(10s)' -format 'csv' > /data/vinh.tran/new/expData/{}/disk_{}_mean.csv" \
            .format(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime(self.started_at)),
                    time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime(self.stopped_at)),
                    host_list,
                    time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime(self.started_at)),
                    time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime(self.stopped_at)),
                    Application.experiment_name,
                    Application.experiment_name)
        print(cmd_query_disk_mean)

        cmd_query_net = "\ninflux -precision rfc3339 -username root -password root " \
                        "-database 'telegraf' -host 'localhost' -execute 'SELECT sum(download_bytes),sum(upload_bytes) FROM (SELECT  derivative(first(\"bytes_recv\"),1s) " \
                        "as \"download_bytes\",derivative(first(\"bytes_sent\"),1s) as \"upload_bytes\"" \
                        "FROM \"telegraf\".\"autogen\".\"net\" WHERE time > '\\''{}'\\'' and time < '\\''{}'\\'' AND host =~ /{}/  " \
                        "GROUP BY \"host\",time(10s)) WHERE time > '\\''{}'\\'' and time < '\\''{}'\\'' GROUP BY host,time(10s)' -format 'csv' > /data/vinh.tran/new/expData/{}/net_{}.csv" \
            .format(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime(self.started_at)),
                    time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime(self.stopped_at)),
                    host_list,
                    time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime(self.started_at)),
                    time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime(self.stopped_at)),
                    Application.experiment_name,
                    Application.experiment_name)
        print(cmd_query_net)

        cmd_query_net_mean = "\ninflux -precision rfc3339 -username root -password root " \
                             "-database 'telegraf' -host 'localhost' -execute 'SELECT sum(download_bytes),sum(upload_bytes) FROM (SELECT  derivative(first(\"bytes_recv\"),1s) " \
                             "as \"download_bytes\",derivative(first(\"bytes_sent\"),1s) as \"upload_bytes\"" \
                             "FROM \"telegraf\".\"autogen\".\"net\" WHERE time > '\\''{}'\\'' and time < '\\''{}'\\'' AND host =~ /{}/  " \
                             "GROUP BY \"host\",time(10s)) WHERE time > '\\''{}'\\'' and time < '\\''{}'\\'' GROUP BY time(10s)' -format 'csv' > /data/vinh.tran/new/expData/{}/net_{}_mean.csv" \
            .format(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime(self.started_at)),
                    time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime(self.stopped_at)),
                    host_list,
                    time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime(self.started_at)),
                    time.strftime('%Y-%m-%dT%H:%M:%SZ', time.localtime(self.stopped_at)),
                    Application.experiment_name,
                    Application.experiment_name)
        print(cmd_query_net_mean)

        subprocess.Popen(
            cmd_query_cpu + " && " + cmd_query_mem + " && " + cmd_query_disk + " && " + cmd_query_net + " && "
            + cmd_query_cpu_mean + " && " + cmd_query_mem_mean + " && " + cmd_query_disk_mean + " && " + cmd_query_net_mean,
            shell=True)

        time.sleep(1)

        with open("/data/vinh.tran/new/expData/{}/cmd.txt".format(Application.experiment_name), 'a') as file:
            file.write("{}\n\n{}\n\n{}\n\n{}\n\n\n\n{}\n\n{}\n\n{}\n\n{}\n".
                       format(cmd_query_cpu, cmd_query_mem, cmd_query_disk, cmd_query_net,
                              cmd_query_cpu_mean, cmd_query_mem_mean, cmd_query_disk_mean, cmd_query_net_mean))

    def get_application_to_schedule(self) -> Application:
        app = self.queue[0]
        if app.n_containers > self.cluster.available_containers():
            raise NoApplicationCanBeScheduled
        return self.queue.pop(0)

    @abstractmethod
    def place_containers(self, app: Application):
        pass

    def _place_random(self, app: Application, n_containers=4):
        nodes = self.cluster.non_full_nodes()
        good_nodes = [
            n for n in nodes
            if len(n.applications()) == 0 or n.applications()[0] != app
        ]
        if len(good_nodes) == 0:
            good_nodes = nodes
        node = good_nodes[np.random.randint(0, len(good_nodes))]
        return self._place(app, node, n_containers)

    @staticmethod
    def _place(app: Application, node: Node, n_containers=4):
        if n_containers <= 0:
            raise ValueError("Can not place {} containers".format(n_containers))
        # print("Place {} on {} ({})".format(app, node, node.available_containers()))

        n = len([t for t in app.tasks if t.node is not None])
        n += 1 if app.node is not None else 0

        for k in range(n, n + n_containers):
            if k < app.n_containers:
                node.add_container(app.containers[k])
                print("Place a task of {} on node {}".format(app, node))

        return k - n + 1


class Random(Scheduler):
    def place_containers(self, app):
        n_containers_scheduled = 0

        while n_containers_scheduled < app.n_containers:
            n_containers_scheduled += self._place_random(app)


class EstimationBenchmark(Random):
    def __init__(self, estimations: List[ComplementarityEstimation], **kwargs):
        super().__init__(estimation=estimations[0], **kwargs)
        self.estimations = estimations

    def update_estimation(self):
        for (apps, usage) in self.cluster.apps_usage():
            if len(apps) > 0 and usage.is_not_idle():
                for rest, out in LeaveOneOut(len(apps)):
                    for estimation in self.estimations:
                        estimation.update_app(apps[out[0]], [apps[i] for i in rest], usage.rate())
        for estimation in self.estimations:
            print(str(estimation))
            estimation.print()

    def on_stop(self):
        delta = self.stopped_at - self.started_at
        print("Queue took {:.0f}'{:.0f} to complete".format(delta // 60, delta % 60))
        for estimation in self.estimations:
            estimation.save(str(estimation))


class RoundRobin(Scheduler):
    def place_containers(self, app: Application):
        empty_nodes = self.cluster.empty_nodes()

        n_containers_scheduled = 0
        print("App {} requires {} containers".format(app, app.n_containers))
        while len(empty_nodes) > 0 and n_containers_scheduled < app.n_containers:
            n_containers_scheduled += self._place(app, empty_nodes.pop())

        while n_containers_scheduled < app.n_containers:
            n_containers_scheduled += self._place_random(app)


class Adaptive(RoundRobin):
    def __init__(self, jobs_to_peek=8, **kwargs):
        super().__init__(**kwargs)
        self.jobs_to_peek = self.jobs_to_peek_arg
        self.print_estimation = True

    def get_application_to_schedule(self):
        scheduled_apps, scheduled_apps_weight = self.cluster.applications(by_name=True)
        available_containers = self.cluster.available_containers()
        index = list(range(min(self.jobs_to_peek, len(self.queue))))
        # Update waiting time for apps in considering queue
        # first schedule round only count the last scheduled app out of 4
        if self.scheduled_apps_num > 2:
            for i in index:
                self.queue[i].waiting_time = self.queue[i].waiting_time + 1

        while len(index) > 0:
            best_i = self.estimation.best_app_index(
                scheduled_apps,
                [self.queue[i] for i in index],
                scheduled_apps_weight
            )

            best_app = self.queue[best_i]

            if best_app.n_containers <= available_containers:
                print("Best app is {} ({}) of queue {}".format(
                    best_app.name,
                    best_i,
                    ",".join([self.queue[i].name for i in index])
                ))
                return self.queue.pop(best_i)

            index.pop(best_i)

        raise NoApplicationCanBeScheduled


class GroupAdaptive(RoundRobin):
    def __init__(self, jobs_to_peek=6, **kwargs):
        super().__init__(**kwargs)
        self.jobs_to_peek = self.jobs_to_peek_arg
        print("Init scheduler - set jobs_to_peek = {}".format(self.jobs_to_peek))
        self.print_estimation = True

    def schedule_application(self) -> Application:
        print("GroupAdaptive-schedule_application()")
        if self.cluster.available_containers()==0:
            raise NoApplicationCanBeScheduled
        app, existing_group = self.get_application_to_schedule()
        print("Marking self.get_app_to_schedule()")
        if app.n_containers > self.cluster.available_containers():
            self.queue = [app] + self.queue
            raise NoApplicationCanBeScheduled

        self.place_containers_with_group(app, existing_group)

        return app

    def place_containers_with_group(self, app: Application, existing_group):
        print("App {} requires {} containers".format(app, app.n_containers))

        if existing_group == -1:
            print("No preferred group to schedule with, check if can schedule on slot 1")
            chosen_slot = JobGroupData.SLOT_1
            if self.cluster.has_application_running():
                print("There are already job running, scheduling on slot 2")
                chosen_slot = JobGroupData.SLOT_2
            app.cluster_slot = chosen_slot
            for address,node in self.cluster.nodes.items():
                if JobGroupData.cluster_slots_index[address] == chosen_slot:
                    self._place(app, node, 4)
        else:
            print("The chosen existing group to co-locate is: {}".format(existing_group))
            co_located_app = None
            running_apps, running_apps_weight = self.cluster.applications(with_full_nodes=False, by_name=True)
            #print(running_apps.__str__())
            for running_app in running_apps:
                if JobGroupData.groupIndexes[running_app.name] == existing_group:
                    print("Choose app {} of group {} to co-locate".format(running_app.name, existing_group))
                    co_located_app = running_app
                    break
            if co_located_app is not None:
                print("The chosen slot to place new job is {}".format(co_located_app.cluster_slot))
                app.cluster_slot = co_located_app.cluster_slot
                for address, node in self.cluster.nodes.items():
                    #print(co_located_app.nodes)
                    #print(address)
                    if address in co_located_app.nodes:
                        self._place(app, node, 4)

        # n_containers_scheduled = 0
        # print("App {} requires {} containers".format(app, app.n_containers))
        # while len(empty_nodes) > 0 and n_containers_scheduled < app.n_containers:
        #     n_containers_scheduled += self._place(app, empty_nodes.pop())
        #
        # while n_containers_scheduled < app.n_containers:
        #     n_containers_scheduled += self._place_random(app)

    def get_application_to_schedule(self):
        global best_i
        scheduled_apps, scheduled_apps_weight = self.cluster.applications(with_full_nodes=False, by_name=True)
        #for app in scheduled_apps:
        #    print(app.__str__())
        available_containers = self.cluster.available_containers()
        index = list(range(min(self.jobs_to_peek, len(self.queue))))
        best_app = None
        # Update waiting time for apps in considering queue
        # first schedule round only count the last scheduled app out of 4
        if self.scheduled_apps_num > 2:
            for i in index:
                self.queue[i].waiting_time = self.queue[i].waiting_time + 1

        while len(index) > 0:
            best_group_to_schedule, best_group_existing = self.estimation.best_app_index(
                scheduled_apps,
                [self.queue[i] for i in index],
                scheduled_apps_weight
            )

            if best_group_to_schedule == -1:
                print("No app is scheduling, pick randomly")
                best_app = self.queue.pop(np.random.randint(0, len(index)))
                print("Choose randomly app {} to schedule".format(best_app.name))
                return best_app, best_group_existing
            else:
                # Pick app from the best group to schedule
                print("Queue to consider: {}".format(",".join([self.queue[i].name for i in index])))
                print("Best app group to schedule: {}".format(best_group_to_schedule))
                print("Best app group existing: {}".format(best_group_existing))
                # print("Index = {}".format(index))
                list_best_jobs_indexes = []
                for i in index:
                    print("Job {} index = {}".format(self.queue[i].name, JobGroupData.groupIndexes[self.queue[i].name]))
                    if JobGroupData.groupIndexes[self.queue[i].name] == best_group_to_schedule:
                        print("Add job {} to list of best apps to choose from best group".format(self.queue[i].name))
                        list_best_jobs_indexes.append(i)
                best_i = list_best_jobs_indexes[np.random.randint(0, len(list_best_jobs_indexes))]
                best_app = self.queue[best_i]
                # print("Best app group to schedule: {}".format(best_group_to_schedule))
                # print("Best app group existing: {}".format(best_group_existing))
                print("Best app is {} ({}) of queue {}".format(
                    best_app.name,
                    best_group_to_schedule,
                    ",".join([self.queue[i].name for i in index])
                ))
                #print("Best app n_containers = {} | available_containers = {}".format(best_app.n_containers,
                #                                                                      available_containers))
            if best_app is None:
                raise NoApplicationCanBeScheduled

            if best_app.n_containers <= available_containers:
                #print("Best app group to schedule: {}".format(best_group_to_schedule))
                #print("Best app group existing: {}".format(best_group_existing))
                #print("Best app is {} ({}) of queue {}".format(
                #    best_app.name,
                #    best_group_to_schedule,
                #    ",".join([self.queue[i].name for i in index])
                #))
                return self.queue.pop(best_i), best_group_existing

            index.pop(best_i)

        raise NoApplicationCanBeScheduled


class GroupAdaptiveExtend(RoundRobin):
    def __init__(self, jobs_to_peek=6, **kwargs):
        super().__init__(**kwargs)
        self.jobs_to_peek = self.jobs_to_peek_arg
        if self.waiting_limit is -1:
            self.waiting_limit = self.jobs_to_peek_arg * 2
        print("Init scheduler - set jobs_to_peek = {}".format(self.jobs_to_peek))
        print("Init scheduler - set waiting_limit = {}".format(self.waiting_limit))
        print("Init scheduler - activate random arrival rate = {}".format(self.activate_random_arrival))
        self.print_estimation = True

    def schedule_application(self) -> Application:
        print("GroupAdaptive-schedule_application()")
        if self.cluster.available_containers()==0:
            raise NoApplicationCanBeScheduled
        app, existing_group = self.get_application_to_schedule()
        print("Marking self.get_app_to_schedule()")
        if app.n_containers > self.cluster.available_containers():
            self.queue = [app] + self.queue
            raise NoApplicationCanBeScheduled

        self.place_containers_with_group(app, existing_group)

        return app

    def place_containers_with_group(self, app: Application, existing_group):
        print("App {} requires {} containers".format(app, app.n_containers))

        if existing_group == -1:
            print("No preferred group to schedule with, check if can schedule on slot 1")
            chosen_slot = JobGroupData.SLOT_1
            if self.cluster.has_application_running():
                print("There are already job running, scheduling on slot 2")
                chosen_slot = JobGroupData.SLOT_2
            app.cluster_slot = chosen_slot
            for address,node in self.cluster.nodes.items():
                if JobGroupData.cluster_slots_index[address] == chosen_slot:
                    self._place(app, node, 4)
        else:
            print("The chosen existing group to co-locate is: {}".format(existing_group))
            co_located_app = None
            running_apps, running_apps_weight = self.cluster.applications(with_full_nodes=False, by_name=True)
            #print(running_apps.__str__())
            for running_app in running_apps:
                if JobGroupData.groupIndexes[running_app.name] == existing_group:
                    print("Choose app {} of group {} to co-locate".format(running_app.name, existing_group))
                    co_located_app = running_app
                    break
            if co_located_app is not None:
                print("The chosen slot to place new job is {}".format(co_located_app.cluster_slot))
                app.cluster_slot = co_located_app.cluster_slot
                for address, node in self.cluster.nodes.items():
                    #print(co_located_app.nodes)
                    #print(address)
                    if address in co_located_app.nodes:
                        self._place(app, node, 4)

        # n_containers_scheduled = 0
        # print("App {} requires {} containers".format(app, app.n_containers))
        # while len(empty_nodes) > 0 and n_containers_scheduled < app.n_containers:
        #     n_containers_scheduled += self._place(app, empty_nodes.pop())
        #
        # while n_containers_scheduled < app.n_containers:
        #     n_containers_scheduled += self._place_random(app)

    def get_waiting_time_based_probability(self, list_apps):
        total_waiting_time = 0
        for app in list_apps:
            total_waiting_time += app.waiting_time
        if total_waiting_time is 0: # first scheduling case
            total_waiting_time = 1 * len(list_apps)
            return [(app.waiting_time + 1)/total_waiting_time for app in list_apps]
        return [app.waiting_time/total_waiting_time for app in list_apps]

    def get_application_to_schedule(self):
        global best_i
        scheduled_apps, scheduled_apps_weight = self.cluster.applications(with_full_nodes=False, by_name=True)
        #for app in scheduled_apps:
        #    print(app.__str__())
        available_containers = self.cluster.available_containers()
        index = list(range(min(self.jobs_to_peek, len(self.queue))))
        best_app = None
        # Update waiting time for apps in considering queue
        # first schedule round only count the last scheduled app out of 4
        if self.scheduled_apps_num > 2:
            late_app = None
            late_index = -1
            for i in index:
                self.queue[i].waiting_time = self.queue[i].waiting_time + 1
                if self.queue[i].waiting_time > self.waiting_limit:
                    print("Job {} waiting time exceeds limit of {}".format(self.queue[i].short_str(), self.waiting_limit))
                    if late_app is None or self.queue[i].waiting_time > late_app.waiting_time:
                        late_app = self.queue[i]
                        late_index = i
            if late_app is not None:
                print("Choose job {} to schedule because of late waiting time".format(late_app.short_str()))
                return self.queue.pop(late_index), JobGroupData.groupIndexes[scheduled_apps[0].name]


        while len(index) > 0:
            best_group_to_schedule, best_group_existing = self.estimation.best_app_index(
                scheduled_apps,
                [self.queue[i] for i in index],
                scheduled_apps_weight
            )

            if best_group_to_schedule == -1:
                print("No app is scheduling, pick randomly")
                best_app = self.queue.pop(np.random.randint(0, len(index)))
                print("Choose randomly app {} to schedule".format(best_app.name))
                return best_app, best_group_existing
            else:
                # Pick app from the best group to schedule
                print("Queue to consider: {}".format(",".join([self.queue[i].short_str() for i in index])))
                print("Best app group to schedule: {}".format(best_group_to_schedule))
                print("Best app group existing: {}".format(best_group_existing))
                # print("Index = {}".format(index))
                list_best_jobs_indexes = []
                list_best_jobs = []
                for i in index:
                    print("Job {} index = {}".format(self.queue[i].name, JobGroupData.groupIndexes[self.queue[i].name]))
                    if JobGroupData.groupIndexes[self.queue[i].name] == best_group_to_schedule:
                        print("Add job {} to list of best apps to choose from best group".format(self.queue[i].name))
                        list_best_jobs_indexes.append(i)
                        list_best_jobs.append(self.queue[i])

                print("Apps to considered in best group: {}".format(",".join([app.short_str() for app in list_best_jobs])))
                waiting_based_probabilities = self.get_waiting_time_based_probability(list_best_jobs)
                print("Waiting based selection probabilities = {}".format(str(waiting_based_probabilities)))
                waiting_indices = np.arange(len(list_best_jobs))
                best_i = list_best_jobs_indexes[np.random.choice(waiting_indices, p=waiting_based_probabilities)]
                print("Chosen index in list best jobs = {}".format(best_i))
                best_app = self.queue[best_i]
                # print("Best app group to schedule: {}".format(best_group_to_schedule))
                # print("Best app group existing: {}".format(best_group_existing))
                print("Best app is {} ({}) of queue {}".format(
                    best_app.name,
                    best_group_to_schedule,
                    ",".join([self.queue[i].name for i in index])
                ))
                #print("Best app n_containers = {} | available_containers = {}".format(best_app.n_containers,
                #                                                                      available_containers))
            if best_app is None:
                raise NoApplicationCanBeScheduled

            if best_app.n_containers <= available_containers:
                #print("Best app group to schedule: {}".format(best_group_to_schedule))
                #print("Best app group existing: {}".format(best_group_existing))
                #print("Best app is {} ({}) of queue {}".format(
                #    best_app.name,
                #    best_group_to_schedule,
                #    ",".join([self.queue[i].name for i in index])
                #))
                return self.queue.pop(best_i), best_group_existing

            index.pop(best_i)

        raise NoApplicationCanBeScheduled
