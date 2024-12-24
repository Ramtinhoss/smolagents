#!/usr/bin/env python
# coding=utf-8

# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import time
from typing import Any, Callable, Dict, List, Optional, Union, Tuple
from dataclasses import dataclass
from rich.syntax import Syntax
from rich.console import Group
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

from .utils import console, parse_code_blob, parse_json_tool_call, truncate_content
from .types import AgentAudio, AgentImage
from .default_tools import FinalAnswerTool
from .llm_engines import HfApiEngine, MessageRole
from .monitoring import Monitor
from .prompts import (
    CODE_SYSTEM_PROMPT,
    JSON_SYSTEM_PROMPT,
    TOOL_CALLING_SYSTEM_PROMPT,
    PLAN_UPDATE_FINAL_PLAN_REDACTION,
    SYSTEM_PROMPT_FACTS,
    SYSTEM_PROMPT_FACTS_UPDATE,
    USER_PROMPT_FACTS_UPDATE,
    USER_PROMPT_PLAN_UPDATE,
    USER_PROMPT_PLAN,
    SYSTEM_PROMPT_PLAN_UPDATE,
    SYSTEM_PROMPT_PLAN,
    MANAGED_AGENT_PROMPT,
)
from .local_python_executor import BASE_BUILTIN_MODULES, LocalPythonInterpreter
from .e2b_executor import E2BExecutor
from .tools import (
    DEFAULT_TOOL_DESCRIPTION_TEMPLATE,
    Tool,
    get_tool_description_with_args,
    Toolbox,
)


class AgentError(Exception):
    """Base class for other agent-related exceptions"""

    def __init__(self, message):
        super().__init__(message)
        self.message = message
        console.print(f"[bold red]{message}[/bold red]")


class AgentParsingError(AgentError):
    """Exception raised for errors in parsing in the agent"""

    pass


class AgentExecutionError(AgentError):
    """Exception raised for errors in execution in the agent"""

    pass


class AgentMaxIterationsError(AgentError):
    """Exception raised for errors in execution in the agent"""

    pass


class AgentGenerationError(AgentError):
    """Exception raised for errors in generation in the agent"""

    pass


@dataclass
class ToolCall:
    name: str
    arguments: Any
    id: str


class AgentStep:
    pass


@dataclass
class ActionStep(AgentStep):
    agent_memory: List[Dict[str, str]] | None = None
    tool_call: ToolCall | None = None
    start_time: float | None = None
    end_time: float | None = None
    iteration: int | None = None
    error: AgentError | None = None
    duration: float | None = None
    llm_output: str | None = None
    observations: str | None = None
    action_output: Any = None


@dataclass
class PlanningStep(AgentStep):
    plan: str
    facts: str


@dataclass
class TaskStep(AgentStep):
    task: str


@dataclass
class SystemPromptStep(AgentStep):
    system_prompt: str


def format_prompt_with_tools(
    toolbox: Toolbox, prompt_template: str, tool_description_template: str
) -> str:
    tool_descriptions = toolbox.show_tool_descriptions(tool_description_template)
    prompt = prompt_template.replace("{{tool_descriptions}}", tool_descriptions)

    if "{{tool_names}}" in prompt:
        prompt = prompt.replace(
            "{{tool_names}}",
            ", ".join([f"'{tool_name}'" for tool_name in toolbox.tools.keys()]),
        )

    return prompt


def show_agents_descriptions(managed_agents: Dict):
    managed_agents_descriptions = """
You can also give requests to team members.
Calling a team member works the same as for calling a tool: simply, the only argument you can give in the call is 'request', a long string explaning your request.
Given that this team member is a real human, you should be very verbose in your request.
Here is a list of the team members that you can call:"""
    for agent in managed_agents.values():
        managed_agents_descriptions += f"\n- {agent.name}: {agent.description}"
    return managed_agents_descriptions


def format_prompt_with_managed_agents_descriptions(
    prompt_template,
    managed_agents,
    agent_descriptions_placeholder: Optional[str] = None,
) -> str:
    if agent_descriptions_placeholder is None:
        agent_descriptions_placeholder = "{{managed_agents_descriptions}}"
    if agent_descriptions_placeholder not in prompt_template:
        print("PROMPT TEMPLLL", prompt_template)
        raise ValueError(
            f"Provided prompt template does not contain the managed agents descriptions placeholder '{agent_descriptions_placeholder}'"
        )
    if len(managed_agents.keys()) > 0:
        return prompt_template.replace(
            agent_descriptions_placeholder, show_agents_descriptions(managed_agents)
        )
    else:
        return prompt_template.replace(agent_descriptions_placeholder, "")


class MultiStepAgent:
    """
    Agent class that solves the given task step by step, using the ReAct framework:
    While the objective is not reached, the agent will perform a cycle of action (given by the LLM) and observation (obtained from the environment).
    """
    def __init__(
        self,
        tools: Union[List[Tool], Toolbox],
        llm_engine: Optional[Callable[[List[Dict[str, str]]], str]] = None,
        system_prompt: Optional[str] = None,
        tool_description_template: Optional[str] = None,
        max_iterations: int = 6,
        tool_parser: Optional[Callable] = None,
        add_base_tools: bool = False,
        verbose: bool = False,
        grammar: Optional[Dict[str, str]] = None,
        managed_agents: Optional[Dict] = None,
        step_callbacks: Optional[List[Callable]] = None,
        monitor_metrics: bool = True,
        planning_interval: Optional[int] = None,
    ):
        if llm_engine is None:
            llm_engine = HfApiEngine()
        if system_prompt is None:
            system_prompt = CODE_SYSTEM_PROMPT
        if tool_parser is None:
            tool_parser = parse_json_tool_call
        self.agent_name = self.__class__.__name__
        self.llm_engine = llm_engine
        self.system_prompt_template = system_prompt
        self.tool_description_template = (
            tool_description_template
            if tool_description_template
            else DEFAULT_TOOL_DESCRIPTION_TEMPLATE
        )
        self.max_iterations = max_iterations
        self.tool_parser = tool_parser
        self.grammar = grammar
        self.planning_interval = planning_interval

        self.managed_agents = {}
        if managed_agents is not None:
            self.managed_agents = {agent.name: agent for agent in managed_agents}

        if isinstance(tools, Toolbox):
            self._toolbox = tools
            if add_base_tools:
                self._toolbox.add_base_tools(
                    add_python_interpreter=(self.__class__ == JsonAgent)
                )
        else:
            self._toolbox = Toolbox(tools, add_base_tools=add_base_tools)
        self._toolbox.add_tool(FinalAnswerTool())

        self.system_prompt = self.initialize_system_prompt()
        self.prompt_messages = None
        self.logs = []
        self.task = None
        self.verbose = verbose

        # Initialize step callbacks
        self.step_callbacks = step_callbacks if step_callbacks is not None else []

        # Initialize Monitor if monitor_metrics is True
        self.monitor = None
        if monitor_metrics:
            self.monitor = Monitor(self.llm_engine)
            self.step_callbacks.append(self.monitor.update_metrics)

    @property
    def toolbox(self) -> Toolbox:
        """Get the toolbox currently available to the agent"""
        return self._toolbox

    def initialize_system_prompt(self):
        self.system_prompt = format_prompt_with_tools(
            self._toolbox,
            self.system_prompt_template,
            self.tool_description_template,
        )
        self.system_prompt = format_prompt_with_managed_agents_descriptions(
            self.system_prompt, self.managed_agents
        )

        return self.system_prompt

    def write_inner_memory_from_logs(
        self, summary_mode: Optional[bool] = False
    ) -> List[Dict[str, str]]:
        """
        Reads past llm_outputs, actions, and observations or errors from the logs into a series of messages
        that can be used as input to the LLM.
        """
        memory = []
        for i, step_log in enumerate(self.logs):
            if isinstance(step_log, SystemPromptStep):
                if not summary_mode:
                    thought_message = {
                        "role": MessageRole.SYSTEM,
                        "content": step_log.system_prompt.strip(),
                    }
                    memory.append(thought_message)

            elif isinstance(step_log, PlanningStep):
                thought_message = {
                    "role": MessageRole.ASSISTANT,
                    "content": "[FACTS LIST]:\n" + step_log.facts.strip(),
                }
                memory.append(thought_message)

                if not summary_mode:
                    thought_message = {
                        "role": MessageRole.ASSISTANT,
                        "content": "[PLAN]:\n" + step_log.plan.strip(),
                    }
                    memory.append(thought_message)

            elif isinstance(step_log, TaskStep):
                task_message = {
                    "role": MessageRole.USER,
                    "content": "New task:\n" + step_log.task,
                }
                memory.append(task_message)

            elif isinstance(step_log, ActionStep):
                if step_log.llm_output is not None and not summary_mode:
                    thought_message = {
                        "role": MessageRole.ASSISTANT,
                        "content": step_log.llm_output.strip(),
                    }
                    memory.append(thought_message)

                if step_log.tool_call is not None:
                    tool_call_message = {
                        "role": MessageRole.ASSISTANT,
                        "content": str(
                            [
                                {
                                    "id": step_log.tool_call.id,
                                    "type": "function",
                                    "function": {
                                        "name": step_log.tool_call.name,
                                        "arguments": step_log.tool_call.arguments,
                                    },
                                }
                            ]
                        ),
                    }
                    memory.append(tool_call_message)

                if step_log.tool_call is None and step_log.error is not None:
                    message_content = (
                        "Error:\n"
                        + str(step_log.error)
                        + "\nNow let's retry: take care not to repeat previous errors! If you have retried several times, try a completely different approach.\n"
                    )
                    tool_response_message = {
                        "role": MessageRole.ASSISTANT,
                        "content": message_content,
                    }
                if step_log.tool_call is not None and (
                    step_log.error is not None or step_log.observations is not None
                ):
                    if step_log.error is not None:
                        message_content = (
                            "Error:\n"
                            + str(step_log.error)
                            + "\nNow let's retry: take care not to repeat previous errors! If you have retried several times, try a completely different approach.\n"
                        )
                    elif step_log.observations is not None:
                        message_content = f"Observation:\n{step_log.observations}"
                    tool_response_message = {
                        "role": MessageRole.TOOL_RESPONSE,
                        "content": f"Call id: {(step_log.tool_call.id if getattr(step_log.tool_call, 'id') else 'call_0')}\n"
                        + message_content,
                    }
                    memory.append(tool_response_message)

        return memory

    def get_succinct_logs(self):
        return [
            {key: value for key, value in log.items() if key != "agent_memory"}
            for log in self.logs
        ]

    def extract_action(self, llm_output: str, split_token: str) -> Tuple[str, str]:
        """
        Parse action from the LLM output

        Args:
            llm_output (`str`): Output of the LLM
            split_token (`str`): Separator for the action. Should match the example in the system prompt.
        """
        try:
            split = llm_output.split(split_token)
            rationale, action = (
                split[-2],
                split[-1],
            )  # NOTE: using indexes starting from the end solves for when you have more than one split_token in the output
        except Exception:
            raise AgentParsingError(
                f"Error: No '{split_token}' token provided in your output.\nYour output:\n{llm_output}\n. Be sure to include an action, prefaced with '{split_token}'!"
            )
        return rationale.strip(), action.strip()


    def provide_final_answer(self, task) -> str:
        """
        This method provides a final answer to the task, based on the logs of the agent's interactions.
        """
        self.prompt_messages = [
            {
                "role": MessageRole.SYSTEM,
                "content": "An agent tried to answer a user query but it got stuck and failed to do so. You are tasked with providing an answer instead. Here is the agent's memory:",
            }
        ]
        self.prompt_messages += self.write_inner_memory_from_logs()[1:]
        self.prompt_messages += [
            {
                "role": MessageRole.USER,
                "content": f"Based on the above, please provide an answer to the following user request:\n{task}",
            }
        ]
        try:
            return self.llm_engine(self.prompt_messages)
        except Exception as e:
            error_msg = f"Error in generating final LLM output: {e}."
            console.print(f"[bold red]{error_msg}[/bold red]")
            return error_msg

    def execute_tool_call(self, tool_name: str, arguments: Dict[str, str]) -> Any:
        """
        Execute tool with the provided input and returns the result.
        This method replaces arguments with the actual values from the state if they refer to state variables.

        Args:
            tool_name (`str`): Name of the Tool to execute (should be one from self.toolbox).
            arguments (Dict[str, str]): Arguments passed to the Tool.
        """
        available_tools = {**self.toolbox.tools, **self.managed_agents}
        if tool_name not in available_tools:
            error_msg = f"Error: unknown tool {tool_name}, should be instead one of {list(available_tools.keys())}."
            console.print(f"[bold red]{error_msg}")
            raise AgentExecutionError(error_msg)

        try:
            if isinstance(arguments, str):
                observation = available_tools[tool_name](arguments)
            elif isinstance(arguments, dict):
                for key, value in arguments.items():
                    if isinstance(value, str) and value in self.state:
                        arguments[key] = self.state[value]
                observation = available_tools[tool_name](**arguments)
            else:
                error_msg = f"Arguments passed to tool should be a dict or string: got a {type(arguments)}."
                console.print(f"[bold red]{error_msg}")
                raise AgentExecutionError(error_msg)
            return observation
        except Exception as e:
            if tool_name in self.toolbox.tools:
                tool_description = get_tool_description_with_args(
                    available_tools[tool_name]
                )
                error_msg = (
                    f"Error in tool call execution: {e}\nYou should only use this tool with a correct input.\n"
                    f"As a reminder, this tool's description is the following:\n{tool_description}"
                )
                console.print(f"[bold red]{error_msg}")
                raise AgentExecutionError(error_msg)
            elif tool_name in self.managed_agents:
                error_msg = (
                    f"Error in calling team member: {e}\nYou should only ask this team member with a correct request.\n"
                    f"As a reminder, this team member's description is the following:\n{available_tools[tool_name]}"
                )
                console.print(f"[bold red]{error_msg}")
                raise AgentExecutionError(error_msg)

    def step(self, log_entry: ActionStep) -> Union[None, Any]:
        """To be implemented in children classes. Should return either None if the step is not final."""
        pass

    def run(
        self,
        task: str,
        stream: bool = False,
        reset: bool = True,
        single_step: bool = False,
        **kwargs,
    ):
        """
        Runs the agent for the given task.

        Args:
            task (`str`): The task to perform.
            stream (`bool`): Wether to run in a streaming way.
            reset (`bool`): Wether to reset the conversation or keep it going from previous run.
            single_step (`bool`): Should the agent run in one shot or multi-step fashion?

        Example:
        ```py
        from smolagents import CodeAgent
        agent = CodeAgent(tools=[])
        agent.run("What is the result of 2 power 3.7384?")
        ```
        """
        self.task = task
        if len(kwargs) > 0:
            self.task += (
                f"\nYou have been provided with these initial arguments: {str(kwargs)}."
            )
        self.state = kwargs.copy()

        self.initialize_system_prompt()
        system_prompt_step = SystemPromptStep(system_prompt=self.system_prompt)

        if reset:
            self.token_count = 0
            self.logs = []
            self.logs.append(system_prompt_step)
        else:
            if len(self.logs) > 0:
                self.logs[0] = system_prompt_step
            else:
                self.logs.append(system_prompt_step)

        console.print(Group(Rule("[bold]New task", characters="="), Text(self.task)))
        self.logs.append(TaskStep(task=self.task))

        if single_step:
            step_start_time = time.time()
            step_log = ActionStep(start_time=step_start_time)
            step_log.end_time = time.time()
            step_log.duration = step_log.end_time - step_start_time

            # Run the agent's step
            result = self.step(step_log)
            return result

        if stream:
            return self.stream_run(self.task)
        else:
            return self.direct_run(self.task)

    def stream_run(self, task: str):
        """
        Runs the agent in streaming mode, yielding steps as they are executed: should be launched only in the `run` method.
        """
        final_answer = None
        iteration = 0
        while final_answer is None and iteration < self.max_iterations:
            step_start_time = time.time()
            step_log = ActionStep(iteration=iteration, start_time=step_start_time)
            try:
                if (
                    self.planning_interval is not None
                    and iteration % self.planning_interval == 0
                ):
                    self.planning_step(
                        task, is_first_step=(iteration == 0), iteration=iteration
                    )
                console.rule(f"[bold]Step {iteration}")

                # Run one step!
                final_answer = self.step(step_log)
            except AgentError as e:
                step_log.error = e
            finally:
                step_log.end_time = time.time()
                step_log.duration = step_log.end_time - step_start_time
                self.logs.append(step_log)
                for callback in self.step_callbacks:
                    callback(step_log)
                iteration += 1
                yield step_log

        if final_answer is None and iteration == self.max_iterations:
            error_message = "Reached max iterations."
            final_step_log = ActionStep(error=AgentMaxIterationsError(error_message))
            self.logs.append(final_step_log)
            final_answer = self.provide_final_answer(task)
            final_step_log.action_output = final_answer
            final_step_log.end_time = time.time()
            final_step_log.duration = step_log.end_time - step_start_time
            for callback in self.step_callbacks:
                callback(final_step_log)
            yield final_step_log

        yield final_answer

    def direct_run(self, task: str):
        """
        Runs the agent in direct mode, returning outputs only at the end: should be launched only in the `run` method.
        """
        final_answer = None
        iteration = 0
        while final_answer is None and iteration < self.max_iterations:
            step_start_time = time.time()
            step_log = ActionStep(iteration=iteration, start_time=step_start_time)
            try:
                if (
                    self.planning_interval is not None
                    and iteration % self.planning_interval == 0
                ):
                    self.planning_step(
                        task, is_first_step=(iteration == 0), iteration=iteration
                    )
                console.rule(f"[bold]Step {iteration}")

                # Run one step!
                final_answer = self.step(step_log)

            except AgentError as e:
                step_log.error = e
            finally:
                step_end_time = time.time()
                step_log.end_time = step_end_time
                step_log.duration = step_end_time - step_start_time
                self.logs.append(step_log)
                for callback in self.step_callbacks:
                    callback(step_log)
                iteration += 1

        if final_answer is None and iteration == self.max_iterations:
            error_message = "Reached max iterations."
            final_step_log = ActionStep(error=AgentMaxIterationsError(error_message))
            self.logs.append(final_step_log)
            final_answer = self.provide_final_answer(task)
            final_step_log.action_output = final_answer
            final_step_log.duration = 0
            for callback in self.step_callbacks:
                callback(final_step_log)

        return final_answer

    def planning_step(self, task, is_first_step: bool, iteration: int):
        """
        Used periodically by the agent to plan the next steps to reach the objective.

        Args:
            task (`str`): The task to perform
            is_first_step (`bool`): If this step is not the first one, the plan should be an update over a previous plan.
            iteration (`int`): The number of the current step, used as an indication for the LLM.
        """
        if is_first_step:
            message_prompt_facts = {
                "role": MessageRole.SYSTEM,
                "content": SYSTEM_PROMPT_FACTS,
            }
            message_prompt_task = {
                "role": MessageRole.USER,
                "content": f"""Here is the task:
```
{task}
```
Now begin!""",
            }

            answer_facts = self.llm_engine([message_prompt_facts, message_prompt_task])

            message_system_prompt_plan = {
                "role": MessageRole.SYSTEM,
                "content": SYSTEM_PROMPT_PLAN,
            }
            message_user_prompt_plan = {
                "role": MessageRole.USER,
                "content": USER_PROMPT_PLAN.format(
                    task=task,
                    tool_descriptions=self._toolbox.show_tool_descriptions(
                        self.tool_description_template
                    ),
                    managed_agents_descriptions=(
                        show_agents_descriptions(self.managed_agents)
                    ),
                    answer_facts=answer_facts,
                ),
            }
            answer_plan = self.llm_engine(
                [message_system_prompt_plan, message_user_prompt_plan],
                stop_sequences=["<end_plan>"],
            )

            final_plan_redaction = f"""Here is the plan of action that I will follow to solve the task:
```
{answer_plan}
```"""
            final_facts_redaction = f"""Here are the facts that I know so far:
```
{answer_facts}
```""".strip()
            self.logs.append(
                PlanningStep(plan=final_plan_redaction, facts=final_facts_redaction)
            )
            console.print(
                Rule("[bold]Initial plan", style="orange"), Text(final_plan_redaction)
            )
        else:  # update plan
            agent_memory = self.write_inner_memory_from_logs(
                summary_mode=False
            )  # This will not log the plan but will log facts

            # Redact updated facts
            facts_update_system_prompt = {
                "role": MessageRole.SYSTEM,
                "content": SYSTEM_PROMPT_FACTS_UPDATE,
            }
            facts_update_message = {
                "role": MessageRole.USER,
                "content": USER_PROMPT_FACTS_UPDATE,
            }
            facts_update = self.llm_engine(
                [facts_update_system_prompt] + agent_memory + [facts_update_message]
            )

            # Redact updated plan
            plan_update_message = {
                "role": MessageRole.SYSTEM,
                "content": SYSTEM_PROMPT_PLAN_UPDATE.format(task=task),
            }
            plan_update_message_user = {
                "role": MessageRole.USER,
                "content": USER_PROMPT_PLAN_UPDATE.format(
                    task=task,
                    tool_descriptions=self._toolbox.show_tool_descriptions(
                        self.tool_description_template
                    ),
                    managed_agents_descriptions=(
                        show_agents_descriptions(self.managed_agents)
                    ),
                    facts_update=facts_update,
                    remaining_steps=(self.max_iterations - iteration),
                ),
            }
            plan_update = self.llm_engine(
                [plan_update_message] + agent_memory + [plan_update_message_user],
                stop_sequences=["<end_plan>"],
            )

            # Log final facts and plan
            final_plan_redaction = PLAN_UPDATE_FINAL_PLAN_REDACTION.format(
                task=task, plan_update=plan_update
            )
            final_facts_redaction = f"""Here is the updated list of the facts that I know:
```
{facts_update}
```"""
            self.logs.append(
                PlanningStep(plan=final_plan_redaction, facts=final_facts_redaction)
            )
            console.print(
                Rule("[bold]Updated plan", style="orange"), Text(final_plan_redaction)
            )


class JsonAgent(MultiStepAgent):
    """
    In this agent, the tool calls will be formulated by the LLM in JSON format, then parsed and executed.
    """

    def __init__(
        self,
        tools: List[Tool],
        llm_engine: Optional[Callable] = None,
        system_prompt: Optional[str] = None,
        grammar: Optional[Dict[str, str]] = None,
        planning_interval: Optional[int] = None,
        **kwargs,
    ):
        if llm_engine is None:
            llm_engine = HfApiEngine()
        if system_prompt is None:
            system_prompt = JSON_SYSTEM_PROMPT
        super().__init__(
            tools=tools,
            llm_engine=llm_engine,
            system_prompt=system_prompt,
            grammar=grammar,
            planning_interval=planning_interval,
            **kwargs,
        )

    def step(self, log_entry: ActionStep) -> Union[None, Any]:
        """
        Perform one step in the ReAct framework: the agent thinks, acts, and observes the result.
        Returns None if the step is not final.
        """
        agent_memory = self.write_inner_memory_from_logs()

        self.prompt_messages = agent_memory

        # Add new step in logs
        log_entry.agent_memory = agent_memory.copy()

        try:
            additional_args = (
                {"grammar": self.grammar} if self.grammar is not None else {}
            )
            llm_output = self.llm_engine(
                self.prompt_messages,
                stop_sequences=["<end_action>", "Observation:"],
                **additional_args,
            )
            log_entry.llm_output = llm_output
        except Exception as e:
            raise AgentGenerationError(f"Error in generating llm_engine output: {e}.")

        if self.verbose:
            console.print(
                Group(
                    Rule(
                        "[italic]Output message of the LLM:",
                        align="left",
                        style="orange",
                    ),
                    Text(llm_output),
                )
            )

        # Parse
        rationale, action = self.extract_action(
            llm_output=llm_output, split_token="Action:"
        )

        try:
            tool_name, tool_arguments = self.tool_parser(action)
        except Exception as e:
            raise AgentParsingError(f"Could not parse the given action: {e}.")

        log_entry.tool_call = ToolCall(
            tool_name=tool_name, tool_arguments=tool_arguments
        )

        # Execute
        console.print(Rule("Agent thoughts:", align="left"), Text(rationale))
        console.print(
            Panel(Text(f"Calling tool: '{tool_name}' with arguments: {tool_arguments}"))
        )
        if tool_name == "final_answer":
            if isinstance(tool_arguments, dict):
                if "answer" in tool_arguments:
                    answer = tool_arguments["answer"]
                else:
                    answer = tool_arguments
            else:
                answer = tool_arguments
            if (
                isinstance(answer, str) and answer in self.state.keys()
            ):  # if the answer is a state variable, return the value
                answer = self.state[answer]
            log_entry.action_output = answer
            return answer
        else:
            if tool_arguments is None:
                tool_arguments = {}
            observation = self.execute_tool_call(tool_name, tool_arguments)
            observation_type = type(observation)
            if observation_type in [AgentImage, AgentAudio]:
                if observation_type == AgentImage:
                    observation_name = "image.png"
                elif observation_type == AgentAudio:
                    observation_name = "audio.mp3"
                # TODO: observation naming could allow for different names of same type

                self.state[observation_name] = observation
                updated_information = f"Stored '{observation_name}' in memory."
            else:
                updated_information = str(observation).strip()
            log_entry.observations = updated_information
            return None


class ToolCallingAgent(MultiStepAgent):
    """
    This agent uses JSON-like tool calls, but to the difference of JsonAgents, it leverages the underlying librarie's tool calling facilities.
    """

    def __init__(
        self,
        tools: List[Tool],
        llm_engine: Optional[Callable] = None,
        system_prompt: Optional[str] = None,
        planning_interval: Optional[int] = None,
        **kwargs,
    ):
        if llm_engine is None:
            llm_engine = HfApiEngine()
        if system_prompt is None:
            system_prompt = TOOL_CALLING_SYSTEM_PROMPT
        super().__init__(
            tools=tools,
            llm_engine=llm_engine,
            system_prompt=system_prompt,
            planning_interval=planning_interval,
            **kwargs,
        )

    def step(self, log_entry: ActionStep) -> Union[None, Any]:
        """
        Perform one step in the ReAct framework: the agent thinks, acts, and observes the result.
        Returns None if the step is not final.
        """
        agent_memory = self.write_inner_memory_from_logs()

        self.prompt_messages = agent_memory

        # Add new step in logs
        log_entry.agent_memory = agent_memory.copy()

        try:
            tool_name, tool_arguments, tool_call_id = self.llm_engine.get_tool_call(
                self.prompt_messages, available_tools=list(self.toolbox._tools.values())
            )
        except Exception as e:
            raise AgentGenerationError(f"Error in generating llm_engine output: {e}.")

        log_entry.tool_call = ToolCall(
            name=tool_name, arguments=tool_arguments, id=tool_call_id
        )

        # Execute
        console.print(
            Panel(Text(f"Calling tool: '{tool_name}' with arguments: {tool_arguments}"))
        )
        if tool_name == "final_answer":
            if isinstance(tool_arguments, dict):
                if "answer" in tool_arguments:
                    answer = tool_arguments["answer"]
                else:
                    answer = tool_arguments
            else:
                answer = tool_arguments
            if (
                isinstance(answer, str) and answer in self.state.keys()
            ):  # if the answer is a state variable, return the value
                answer = self.state[answer]
            log_entry.action_output = answer
            return answer
        else:
            if tool_arguments is None:
                tool_arguments = {}
            observation = self.execute_tool_call(tool_name, tool_arguments)
            observation_type = type(observation)
            if observation_type in [AgentImage, AgentAudio]:
                if observation_type == AgentImage:
                    observation_name = "image.png"
                elif observation_type == AgentAudio:
                    observation_name = "audio.mp3"
                # TODO: observation naming could allow for different names of same type

                self.state[observation_name] = observation
                updated_information = f"Stored '{observation_name}' in memory."
            else:
                updated_information = str(observation).strip()
            log_entry.observations = updated_information
            return None


class CodeAgent(MultiStepAgent):
    """
    In this agent, the tool calls will be formulated by the LLM in code format, then parsed and executed.
    """

    def __init__(
        self,
        tools: List[Tool],
        llm_engine: Optional[Callable] = None,
        system_prompt: Optional[str] = None,
        grammar: Optional[Dict[str, str]] = None,
        additional_authorized_imports: Optional[List[str]] = None,
        planning_interval: Optional[int] = None,
        use_e2b_executor: bool = False,
        **kwargs,
    ):
        if llm_engine is None:
            llm_engine = HfApiEngine()
        if system_prompt is None:
            system_prompt = CODE_SYSTEM_PROMPT
        super().__init__(
            tools=tools,
            llm_engine=llm_engine,
            system_prompt=system_prompt,
            grammar=grammar,
            planning_interval=planning_interval,
            **kwargs,
        )

        self.additional_authorized_imports = (
            additional_authorized_imports if additional_authorized_imports else []
        )
        if use_e2b_executor and len(self.managed_agents) > 0:
            raise Exception(
                f"You passed both {use_e2b_executor=} and some managed agents. Managed agents is not yet supported with remote code execution."
            )

        all_tools = {**self.toolbox.tools, **self.managed_agents}
        if use_e2b_executor:
            self.python_executor = E2BExecutor(
                self.additional_authorized_imports, list(all_tools.values())
            )
        else:
            self.python_executor = LocalPythonInterpreter(
                self.additional_authorized_imports, all_tools
            )
        self.authorized_imports = list(
            set(BASE_BUILTIN_MODULES) | set(self.additional_authorized_imports)
        )
        if "{{authorized_imports}}" not in self.system_prompt:
            raise AgentError(
                "Tag '{{authorized_imports}}' should be provided in the prompt."
            )
        self.system_prompt = self.system_prompt.replace(
            "{{authorized_imports}}", str(self.authorized_imports)
        )

    def step(self, log_entry: ActionStep) -> Union[None, Any]:
        """
        Perform one step in the ReAct framework: the agent thinks, acts, and observes the result.
        Returns None if the step is not final.
        """
        agent_memory = self.write_inner_memory_from_logs()

        self.prompt_messages = agent_memory.copy()

        # Add new step in logs
        log_entry.agent_memory = agent_memory.copy()

        try:
            additional_args = (
                {"grammar": self.grammar} if self.grammar is not None else {}
            )
            llm_output = self.llm_engine(
                self.prompt_messages,
                stop_sequences=["<end_action>", "Observation:"],
                **additional_args,
            )
            log_entry.llm_output = llm_output
        except Exception as e:
            raise AgentGenerationError(f"Error in generating llm_engine output: {e}.")

        if self.verbose:
            console.print(
                Group(
                    Rule(
                        "[italic]Output message of the LLM:",
                        align="left",
                        style="orange",
                    ),
                    Syntax(llm_output, lexer="markdown", theme="github-dark"),
                )
            )

        # Parse
        try:
            rationale, raw_code_action = self.extract_action(
                llm_output=llm_output, split_token="Code:"
            )
        except Exception as e:
            console.print(
                f"Error in extracting action, trying to parse the whole output. Error trace: {e}"
            )
            rationale, raw_code_action = llm_output, llm_output

        try:
            code_action = parse_code_blob(raw_code_action)
        except Exception as e:
            error_msg = f"Error in code parsing: {e}. Make sure to provide correct code"
            raise AgentParsingError(error_msg)

        log_entry.tool_call = ToolCall(
            name="python_interpreter",
            arguments=code_action,
            id=f"call_{len(self.logs)}",
        )

        # Execute
        if self.verbose:
            console.print(
                Group(Rule("[italic]Agent thoughts", align="left"), Text(rationale))
            )

        console.print(
            Panel(
                Syntax(code_action, lexer="python", theme="github-dark"),
                title="[bold]Agent is executing the code below:",
                title_align="left",
            )
        )

        try:
            output, execution_logs = self.python_executor(
                code_action,
            )
            if len(execution_logs) > 0:
                console.print(
                    Group(Text("Execution logs:", style="bold"), Text(execution_logs))
                )
            observation = "Execution logs:\n" + execution_logs
            if output is not None:
                truncated_output = truncate_content(str(output))
                console.print(
                    Group(
                        Text("Last output from code snippet:", style="bold"),
                        Text(truncated_output),
                    )
                )
                observation += "Last output from code snippet:\n" + truncate_content(
                    str(output)
                )
            log_entry.observations = observation
        except Exception as e:
            error_msg = f"Code execution failed due to the following error:\n{str(e)}"
            if "'dict' object has no attribute 'read'" in str(e):
                error_msg += "\nYou get this error because you passed a dict as input for one of the arguments instead of a string."
            raise AgentExecutionError(error_msg)
        for line in code_action.split("\n"):
            if line[: len("final_answer")] == "final_answer":
                console.print(
                    Group(
                        Text("Final answer:", style="bold"),
                        Text(str(output), style="bold green"),
                    )
                )
                log_entry.action_output = output
                return output


class ManagedAgent:
    def __init__(
        self,
        agent,
        name,
        description,
        additional_prompting: Optional[str] = None,
        provide_run_summary: bool = False,
        managed_agent_prompt: Optional[str] = None,
    ):
        self.agent = agent
        self.name = name
        self.description = description
        self.additional_prompting = additional_prompting
        self.provide_run_summary = provide_run_summary
        self.managed_agent_prompt = (
            managed_agent_prompt if managed_agent_prompt else MANAGED_AGENT_PROMPT
        )

    def write_full_task(self, task):
        """Adds additional prompting for the managed agent, like 'add more detail in your answer'."""
        full_task = self.managed_agent_prompt.format(name=self.name, task=task)
        if self.additional_prompting:
            full_task = full_task.replace(
                "\n{{additional_prompting}}", self.additional_prompting
            ).strip()
        else:
            full_task = full_task.replace("\n{{additional_prompting}}", "").strip()
        return full_task

    def __call__(self, request, **kwargs):
        full_task = self.write_full_task(request)
        output = self.agent.run(full_task, **kwargs)
        if self.provide_run_summary:
            answer = (
                f"Here is the final answer from your managed agent '{self.name}':\n"
            )
            answer += str(output)
            answer += f"\n\nFor more detail, find below a summary of this agent's work:\nSUMMARY OF WORK FROM AGENT '{self.name}':\n"
            for message in self.agent.write_inner_memory_from_logs(summary_mode=True):
                content = message["content"]
                answer += "\n" + truncate_content(str(content)) + "\n---"
            answer += f"\nEND OF SUMMARY OF WORK FROM AGENT '{self.name}'."
            return answer
        else:
            return output


__all__ = [
    "AgentError",
    "BaseAgent",
    "ManagedAgent",
    "MultiStepAgent",
    "CodeAgent",
    "JsonAgent",
    "Toolbox",
]