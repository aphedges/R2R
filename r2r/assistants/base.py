import asyncio
from abc import ABCMeta
from typing import AsyncGenerator, Generator, Optional

from r2r.base import (
    Assistant,
    AsyncSyncMeta,
    LLMChatCompletion,
    LLMChatCompletionChunk,
    Message,
    syncable,
)


class CombinedMeta(AsyncSyncMeta, ABCMeta):
    pass


def sync_wrapper(async_gen):
    loop = asyncio.get_event_loop()

    def wrapper():
        try:
            while True:
                try:
                    yield loop.run_until_complete(async_gen.__anext__())
                except StopAsyncIteration:
                    break
        finally:
            loop.run_until_complete(async_gen.aclose())

    return wrapper()


class R2RAssistant(Assistant, metaclass=CombinedMeta):
    @syncable
    async def arun(
        self,
        system_instruction: Optional[str] = None,
        messages: Optional[list[Message]] = None,
        *args,
        **kwargs,
    ) -> list[LLMChatCompletion]:
        if system_instruction or not self.conversation:
            self._completed = False
            self._setup(system_instruction)

        if messages:
            self.conversation.extend(messages)

        while not self._completed:
            generation_config = self.get_generation_config()
            response = await self.llm_provider.aget_completion(
                self.conversation,
                generation_config,
            )
            await self.process_llm_response(response, *args, **kwargs)

        return self.conversation

    async def process_llm_response(
        self, response: LLMChatCompletion, *args, **kwargs
    ) -> str:
        message = response.choices[0].message
        if message.function_call:
            await self.handle_function_call(
                message.function_call.name,
                message.function_call.arguments,
                *args,
                **kwargs,
            )
            return await self.arun(*args, **kwargs)
        else:
            self.conversation.append(
                Message(role="assistant", content=message.content)
            )
            self._completed = True
            return message.content


class R2RStreamingAssistant(Assistant):
    async def arun(
        self,
        system_instruction: Optional[str] = None,
        messages: Optional[list[Message]] = None,
        *args,
        **kwargs,
    ) -> AsyncGenerator[str, None]:
        if system_instruction or not self.conversation:
            self._completed = False
            self._setup(system_instruction)

        if messages:
            self.conversation.extend(messages)

        while not self._completed:
            generation_config = self.get_generation_config()
            stream = self.llm_provider.get_completion_stream(
                self.conversation,
                generation_config,
            )
            async for chunk in self.process_llm_response(
                stream, *args, **kwargs
            ):
                yield chunk

    def run(
        self, system_instruction, messages, *args, **kwargs
    ) -> Generator[str, None, None]:
        return sync_wrapper(
            self.arun(system_instruction, messages, *args, **kwargs)
        )

    async def process_llm_response(
        self, stream: LLMChatCompletionChunk, *args, **kwargs
    ) -> AsyncGenerator[str, None]:
        function_name = None
        function_arguments = ""
        content_buffer = ""

        for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.function_call:
                if delta.function_call.name:
                    function_name = delta.function_call.name
                if delta.function_call.arguments:
                    function_arguments += delta.function_call.arguments
            elif delta.content:
                content_buffer += delta.content
                yield delta.content

            if chunk.choices[0].finish_reason == "function_call":
                yield "<function_call>"
                yield f"<name>{function_name}</name>"
                yield f"<arguments>{function_arguments}</arguments>"
                results = await self.handle_function_call(
                    function_name, function_arguments, *args, **kwargs
                )
                yield f"<results>{results}</results>"

                function_name = None
                function_arguments = ""

                self.arun(*args, **kwargs)

            elif chunk.choices[0].finish_reason == "stop":
                if content_buffer:
                    self.conversation.append(
                        Message(role="assistant", content=content_buffer)
                    )
                self._completed = True
