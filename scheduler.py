from complementarity import ComplementarityEstimation
from cluster import Cluster, Application
from abc import ABCMeta, abstractmethod
from sklearn.cross_validation import LeaveOneOut
from repeated_timer import RepeatedTimer


class Scheduler(metaclass=ABCMeta):
    def __init__(self, estimation: ComplementarityEstimation, cluster: Cluster):
        self._queue = []
        self._estimation = estimation
        self._cluster = cluster
        self.timer = RepeatedTimer(60, self.update_estimation)

    def update_estimation(self):
        for (apps, usage) in self._cluster.apps_usage():
            if len(apps) > 0:
                rate = self.usage2rate(usage)
                for rest, out in LeaveOneOut(len(apps)):
                    self._estimation.update_job(apps[out][0], apps[rest], rate)

    def stop_updating_estimation(self):
        self.timer.cancel()

    @staticmethod
    def usage2rate(usage):
        return usage.sum()

    def best_app_index(self, scheduled_apps, apps):
        return self._estimation.argsort_jobs(scheduled_apps, apps)

    def add(self, app: Application):
        self._queue.append(app)

    @abstractmethod
    def schedule(self):
        pass


class QueueModificationScheduler(Scheduler):
    def __init__(self, *args, running_jobs=2, jobs_to_peek=5):
        super().__init__(*args)
        self.running_jobs = running_jobs
        self.jobs_to_peek = jobs_to_peek

    def schedule(self):
        n = len(jobs)
        scheduled_jobs = [jobs.pop(0)]

        while len(scheduled_jobs) < n:
            index = self.best_app_index(
                scheduled_jobs[-self.running_jobs:],
                jobs[:self.jobs_to_peek]
            )
            scheduled_jobs.append(jobs.pop(index))

        return scheduled_jobs

