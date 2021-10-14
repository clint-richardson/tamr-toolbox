from datetime import datetime
import json
import os
from dataclasses import dataclass
from dataclasses_json import dataclass_json
from typing import Dict, List
import logging

from tamr_unify_client import Client
from tamr_toolbox.models.data_type import JsonDict

from tamr_toolbox.workflow.concurrent.Graph import (
    Graph,
    get_projects_by_tier,
    get_all_downstream_nodes,
    get_successors,
    get_predecessors,
)
from tamr_toolbox.workflow.concurrent import PlanNodeStatus
from tamr_toolbox.workflow.concurrent.PlanStatus import PlanStatus, from_planner
from tamr_toolbox.workflow.concurrent.PlanNode import PlanNode, run_next_step, monitor


LOGGER = logging.getLogger(__name__)


@dataclass_json
@dataclass
class Planner:
    """
    A dataclass to hold the plan, the starting tier, and the mode of execution.
    The plan is a json dict where each key is a project name and the value is a PlanNode object

    The starting tier is the tier at which to start execution. All jobs at lower tiers are
     marked as skippable.

    The graph is the graph that contains the backing project dependencies.
    """

    plan: Dict[str, PlanNode]
    starting_tier: int
    graph: Graph
    output_config: JsonDict


def from_graph(
    graph: Graph,
    *,
    tamr_client: Client,
    starting_tier: int = 0,
    output_config: JsonDict = None,
    train=False,
) -> Planner:
    """
    Creates a Planner class from a Graph. The plan object is a json dict specifying how
    the plan can be executed and its status.

    Args:
        graph: the dataset dependency graph to use to create the planner
        tamr_client: the tamr client object associated with the instance for which
            to create the plan
        starting_tier: the tier at which to start executing the plan, every job at lower
            tiers is skipped and marked
        as skippable
        output_config: a dict for how to configure output jobs
        train: global config for whether or not to 'apply feedback'/train the model in
            the workflows

    Returns:
        Planner instance
    """

    # graphs don't store tamr project objects themselves just the names so need to build lookup
    tamr_projects = {x.name: x for x in tamr_client.projects.stream()}

    # start with the project tier dict from the graph
    tier_graph = get_projects_by_tier(graph)

    # start building the plan
    plan = {}
    for tier, project_list in tier_graph.items():
        for num, project_name in enumerate(project_list):
            # mark things as skippable if the tier is less than the starting tier
            if tier < starting_tier:
                status = PlanNodeStatus.PlanNodeStatus.SKIPPABLE
            elif tier == starting_tier:
                status = PlanNodeStatus.PlanNodeStatus.RUNNABLE
            else:
                status = PlanNodeStatus.PlanNodeStatus.PLANNED
            plan[project_name] = PlanNode(
                priority=(100 * tier) + num,
                status=status,
                name=project_name,
                current_op=None,
                operations=None,
                project=tamr_projects[project_name],
                train=train,
            )

    return Planner(
        plan=plan, starting_tier=starting_tier, graph=graph, output_config=output_config
    )


def update_plan(planner: Planner, *, plan_node: PlanNode) -> Planner:
    """
    Create an new planner object with updated status from a set of plan nodes
    Args:
        planner: the original planner
        plan_node: an updated set of plan nodes

    Returns:
        a copy of the original planner object with an updated status
    """
    # first just update the status of that node
    original_plan = planner.plan
    updated_plan = dict(original_plan)
    LOGGER.info(
        f"Updating plan with changed project status: {plan_node.name} "
        f"status changed to {plan_node.status}"
    )
    plan_node_name = plan_node.name
    node_status = PlanNodeStatus.from_plan_node(plan_node)
    updated_plan[plan_node_name].status = node_status
    updated_plan[plan_node_name].operations = plan_node.operations

    # now find downstream affects
    downstream_nodes = get_all_downstream_nodes(planner.graph, plan_node_name)
    # if status == failed then easy to update them all to blocked
    if node_status == PlanNodeStatus.PlanNodeStatus.FAILED:
        for node in downstream_nodes:
            updated_plan[node].status = PlanNodeStatus.PlanNodeStatus.BLOCKED
    # else if update is skippable or successful then need to see if we can mark them as runnable
    elif (
        node_status == PlanNodeStatus.PlanNodeStatus.SUCCEEDED
        or node_status == PlanNodeStatus.PlanNodeStatus.SKIPPABLE
    ):
        # first get immediate downstream nodes
        successor_nodes = get_successors(planner.graph, plan_node_name)
        # for each of these get the predecessors and if all predecessors are now
        # succeeded/skippable mark as runnable
        for successor in successor_nodes:
            predecessor_nodes = get_predecessors(planner.graph, successor)
            if all(
                updated_plan[x].status == PlanNodeStatus.PlanNodeStatus.SUCCEEDED
                or updated_plan[x].status == PlanNodeStatus.PlanNodeStatus.SKIPPABLE
                for x in predecessor_nodes
            ):
                updated_plan[successor].status = PlanNodeStatus.PlanNodeStatus.RUNNABLE

    # no other status should change things
    # so do nothing else

    return Planner(
        plan=updated_plan,
        graph=planner.graph,
        starting_tier=planner.starting_tier,
        output_config=planner.output_config,
    )


def execute(
    planner: Planner, tamr: Client, *, concurrency_level: int = 2, save_state: bool = False
) -> Planner:
    """
    Executes the plan

    Args:
        planner: The planner object whose plan will be executed
        tamr: the tamr client to use
        concurrency_level: the number of concurrent jobs to run at once
        save_state: whether or not to save the plan state to json after each update

    Returns:
        the planner object after execution
    """

    # get the plan and sort by priority
    plan = planner.plan
    sorted_jobs = [v for k, v in sorted(plan.items(), key=lambda x: x[1].priority)]
    # assume you could be given a partially executed plan so create both running and runnable
    runnable_nodes = [x for x in sorted_jobs if x.status == PlanNodeStatus.PlanNodeStatus.RUNNABLE]
    running_nodes = [x for x in sorted_jobs if x.status == PlanNodeStatus.PlanNodeStatus.RUNNING]

    # check status and run if runnable or planned
    plan_status = from_planner(planner)
    if plan_status == PlanStatus.PLANNED or plan_status == PlanStatus.RUNNING:
        LOGGER.info(
            "projects with currently running jobs: "
            f"{','.join([x.name for x in running_nodes]) or 'None'}"
        )
        # make sure there are fewer jobs running than concurrency specified
        num_to_submit = concurrency_level - len(running_nodes)
        LOGGER.info(f"Have room in queue for {num_to_submit} jobs:")

        # slice runnable jobs to get the ones to submit
        # this line is for type hinting
        nodes_to_submit: List[PlanNode] = []
        if len(runnable_nodes) >= num_to_submit:
            nodes_to_submit = [x for x in runnable_nodes[0:num_to_submit]]
        else:
            nodes_to_submit = [x for x in runnable_nodes]

        LOGGER.info(f"submitting jobs for projects: [{','.join(x.name for x in nodes_to_submit)}]")
        # create the list of nodes to monitor
        # note that this returns a list of plan nodes AND triggers the job
        nodes_to_monitor = [run_next_step(x) for x in nodes_to_submit]
        # extend jobs_to_monitor to include running jobs
        LOGGER.info(
            f"Adding currently running projects to queue: {[x.name for x in running_nodes]}"
        )
        nodes_to_monitor.extend(
            [
                PlanNode(
                    name=x.name,
                    status=x.status,
                    priority=0,
                    current_op=x.current_op,
                    project=x.project,
                    operations=x.operations,
                    steps_to_run=x.steps_to_run,
                    current_step=x.current_step,
                )
                for x in running_nodes
            ]
        )

        # TODO: revisit this logic
        # there are potentially jobs that were not submitted
        # because the dataset is already streamable
        # for these simply filter out and update plan
        # first find them
        noop_jobs = [x for x in nodes_to_monitor if any(["No-op" in y for y in x.operations])]
        # then filter these out
        nodes_to_monitor = [x for x in nodes_to_monitor if x not in noop_jobs]
        # now update the plan for the no-ops
        for noop_job in noop_jobs:
            planner = update_plan(planner, plan_node=noop_job)

        # now monitor the ones that really submit a job
        # this function returns when there is any change in state
        nodes_to_monitor = monitor(nodes_to_monitor)
        LOGGER.info(f"Got updated set of jobs: {nodes_to_monitor}")
        # now update the plan - only monitored jobs should have a change in status
        for job in nodes_to_monitor:
            planner = update_plan(planner, plan_node=job)

        LOGGER.info(f"after recent update plan status is {from_planner(planner)}")

        # if save state then save a copy of the plan:
        # todo: refactor save state to its own function
        if save_state:
            now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            basedir = os.path.dirname(os.path.abspath(__file__))
            with open(f"{basedir}/../../logs/planner_{now}.json", "w") as outfile:
                outfile.write(
                    json.dumps(
                        [
                            {"name": v.name, "status": v.status, "priority": v.priority}
                            for k, v in planner.plan.items()
                        ]
                    )
                )
        # planner is updated so now try to execute it again
        planner = execute(planner, tamr=tamr, concurrency_level=concurrency_level)
        return planner

    # if planner isn't runnable and there were no export processes then exit
    else:
        LOGGER.info(f"plan status is {plan_status} so returning")
        return planner