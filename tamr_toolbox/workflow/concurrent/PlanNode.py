from dataclasses import dataclass, field
from dataclasses_json import dataclass_json
from typing import List, Union
import time
import logging

from tamr_unify_client.project.resource import Project
from tamr_unify_client.operation import Operation
from tamr_toolbox.workflow.concurrent.PlanNodeStatus import PlanNodeStatus, from_tamr_op
from tamr_toolbox.models.project_type import ProjectType
from tamr_toolbox.project import categorization, mastering, schema_mapping, golden_records

LOGGER = logging.getLogger(__name__)

WORKFLOW_MAP = {
    ProjectType.SCHEMA_MAPPING_RECOMMENDATIONS: {
        schema_mapping.Steps.UPDATE_UNIFIED_DATASET: schema_mapping.jobs.update_unified_dataset
    },
    ProjectType.DEDUP: {
        mastering.Steps.UPDATE_UNIFIED_DATASET: mastering.jobs.update_unified_dataset,
        mastering.Steps.GENERATE_PAIRS: mastering.jobs.generate_pairs,
        mastering.Steps.TRAIN_MASTERING_MODEL: mastering.jobs.apply_feedback,
        mastering.Steps.UPDATE_HIGH_IMPACT_PAIRS: mastering.jobs.update_pair_predictions,
        mastering.Steps.UPDATE_CLUSTERS: mastering.jobs.update_clusters,
        mastering.Steps.PUBLISH_CLUSTERS: mastering.jobs.publish_clusters,
    },
    ProjectType.CATEGORIZATION: {
        categorization.Steps.UPDATE_UNIFIED_DATASET: categorization.jobs.update_unified_dataset,
        categorization.Steps.APPLY_FEEDBACK: categorization.jobs.apply_feedback,
        categorization.Steps.UPDATE_RESULTS_ONLY: categorization.jobs.update_results_only,
    },
    ProjectType.GOLDEN_RECORDS: {
        golden_records.Steps.PROFILE_GOLDEN_RECORDS:
            golden_records.jobs.update_input_dataset_profiling_information,
        golden_records.Steps.UPDATE_GOLDEN_RECORDS: golden_records.jobs.update_golden_records,
        golden_records.Steps.PUBLISH_GOLDEN_RECORDS: golden_records.jobs.publish_golden_records,
    },
}


@dataclass_json
@dataclass
class PlanNode:
    """
    Dataclass for the node of a Planner object
    - contains method for setting list of operations based on project type
    """

    name: str
    operations: List[Operation]
    project: Project
    priority: int
    current_op: Operation
    status: PlanNodeStatus = PlanNodeStatus.RUNNABLE
    train: bool = False
    project_type: ProjectType = field(init=False)
    project_steps: Union[
        List[schema_mapping.Steps],
        List[mastering.Steps],
        List[golden_records.Steps],
        List[categorization.Steps],
    ] = field(init=False)
    current_step: Union[
        schema_mapping.Steps, mastering.Steps, categorization.Steps, golden_records.Steps
    ] = None
    steps_to_run: Union[
        List[schema_mapping.Steps],
        List[mastering.Steps],
        List[golden_records.Steps],
        List[categorization.Steps],
    ] = None

    def __post_init__(self):
        # set the project type
        self.project_type = ProjectType[self.project.type]

        # then set project steps based on project type
        # first for schema mapping projects
        if self.project_type == ProjectType.SCHEMA_MAPPING_RECOMMENDATIONS:
            self.project_steps = [schema_mapping.Steps.UPDATE_UNIFIED_DATASET]
        # now for mastering projects
        elif self.project_type == ProjectType.DEDUP:
            if self.train:
                self.project_steps = [
                    mastering.Steps.UPDATE_UNIFIED_DATASET,
                    mastering.Steps.GENERATE_PAIRS,
                    mastering.Steps.TRAIN_MASTERING_MODEL,
                    mastering.Steps.UPDATE_HIGH_IMPACT_PAIRS,
                    mastering.Steps.UPDATE_CLUSTERS,
                    mastering.Steps.PUBLISH_CLUSTERS,
                ]
            else:
                self.project_steps = [
                    mastering.Steps.UPDATE_UNIFIED_DATASET,
                    mastering.Steps.GENERATE_PAIRS,
                    mastering.Steps.UPDATE_HIGH_IMPACT_PAIRS,
                    mastering.Steps.UPDATE_CLUSTERS,
                    mastering.Steps.PUBLISH_CLUSTERS,
                ]
        # now for categorization projects
        elif self.project_type == ProjectType.CATEGORIZATION:
            if self.train:
                self.project_steps = [
                    categorization.Steps.UPDATE_UNIFIED_DATASET,
                    categorization.Steps.APPLY_FEEDBACK,
                    categorization.Steps.UPDATE_RESULTS_ONLY,
                ]
            else:
                self.project_steps = [
                    categorization.Steps.UPDATE_UNIFIED_DATASET,
                    categorization.Steps.UPDATE_RESULTS_ONLY,
                ]
        # finally for golden record projects
        elif self.project_type == ProjectType.GOLDEN_RECORDS:
            self.project_steps = [
                golden_records.Steps.PROFILE_GOLDEN_RECORDS,
                golden_records.Steps.UPDATE_GOLDEN_RECORDS,
                golden_records.Steps.PUBLISH_GOLDEN_RECORDS,
            ]

        else:
            raise NotImplementedError(
                f"Don't know how to create a PlanNode object for project type {self.project_type}"
            )


def poll(plan_node: PlanNode) -> PlanNode:
    """
    Polls the status of the current_op object and returns a new PlanNode with updated status

    Args:
        plan_node: the PlanNode object to poll

    Returns:
        Copy of the original plan node with status updated based on the status of the current_op
    """
    updated_op = plan_node.current_op.poll()
    updated_plan_node_status = from_tamr_op(updated_op)
    return PlanNode(
        name=plan_node.name,
        operations=plan_node.operations,
        project=plan_node.project,
        priority=plan_node.priority,
        current_op=updated_op,
        status=updated_plan_node_status,
        current_step=plan_node.current_step,
        steps_to_run=plan_node.steps_to_run,
    )


def run_next_step(plan_node: PlanNode) -> PlanNode:
    """
    Takes a plan node and runs the project

    Args:
        plan_node: the node to run

    Returns:
        updated plan node
    """

    # if current_step is None this node has never been run, so set it and the steps_to_run
    current_step = plan_node.current_step
    if current_step is None:
        current_step = plan_node.project_steps[0]
        steps_to_run = plan_node.project_steps[1:]
    # else we are on step 2+ and so current_step needs to be set to steps_to_run[0]
    # and steps_to_run needs to have the zeroth step removed
    else:
        current_step = plan_node.steps_to_run[0]
        steps_to_run = plan_node.steps_to_run[1:]

    # We only call methods that return a list with one and only one operation
    current_op = WORKFLOW_MAP[plan_node.project_type][current_step](
        plan_node.project, process_asynchronously=True
    )[0]
    # handle case where operations is empty list (nothing has been run)
    if plan_node.operations is None:
        operations_list = [current_op]
    else:
        operations_list = plan_node.operations.append(current_op)
    # don't forget to update the status
    status = from_tamr_op(current_op)
    return PlanNode(
        name=plan_node.name,
        operations=operations_list,
        current_op=current_op,
        priority=plan_node.priority,
        project=plan_node.project,
        steps_to_run=steps_to_run,
        status=status,
    )


def monitor(
    nodes: List[PlanNode], *, timeout: int = 2, polling_interval: int = 30
) -> List[PlanNode]:
    """
    Monitors the status of a list of PlanNodes, when one on that list changes to
        either failed/succeeded/cancelled it returns the list

    Args:
        nodes: list of nodes to monitor
        timeout: number of days to poll before timing out for change in job status
        polling_interval: the amount of time in seconds to wait between polling

    Returns:
        the same list of planNodes with updated statuses
    """
    # if empty then return immediate and log a warning
    if not nodes:
        LOGGER.warning("Jobs list is empty! Something is probably wrong.")
        return []

    start_time = time.time()
    timeout_in_seconds = 3600 * 24 * timeout

    # get initial statuses
    initial_statuses = {x.resource_id: x.state for x in nodes}

    # get project names for logging
    running_projects = "\n".join([x.name for x in nodes])
    LOGGER.info(
        f"starting to monitor projects:\n {running_projects}\n with timeout of {timeout} days"
    )
    # poll only as long as we haven't gotten past timeout
    while (time.time() - start_time) < timeout_in_seconds:

        # sleep at beginning of loop for polling interval
        time.sleep(polling_interval)

        # poll the jobs and get updated nodes
        updated_nodes = [poll(x) for x in nodes]
        # generate updated statuses
        updated_statuses = {x.name: x.status for x in updated_nodes}

        # if status has changed return list
        if initial_statuses != updated_statuses:
            return updated_nodes

    # if we got here it timed out and raise runtime error
    error_message = f"Monitoring jobs:\n {running_projects}\n timed out after {timeout} days!"
    LOGGER.error(error_message)
    raise RuntimeError(error_message)