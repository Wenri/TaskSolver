class ToolCallException(Exception):
    """
    @GPT4-doc-begin
    raised in tools when the tool usage raised an error
    @GPT4-doc-end
    """
    pass


class GPTOutputParseException(Exception):
    """ 
    @GPT4-doc-begin
    raised in parsers when input is not parseable.
    @GPT4-doc-end
    """
    pass


class GPTServerError(Exception):
    """
    @GPT4-doc-begin
    raised in ask() when GPT api returns an error message
    @GPT4-doc-end
    """
    pass


class GPTMaxTriesExceededException(Exception):
    """ 
    @GPT4-doc-begin
    raised in ask() when max attempt threshold has been exceeded.
    @GPT4-doc-end
    """
    def __init__(
        self,
        *args,
        raw_response=None,
        response_metadata=None,
        request_payload=None,
    ):
        super().__init__(*args)
        self.raw_response = raw_response
        self.response_metadata = response_metadata
        self.request_payload = request_payload

class InvalidParsedAnswer(Exception):
    """ 
    @GPT4-doc-begin
    raised in the visualizer when input ParsedAnswer can't be made into
    a valid visualization.
    @GPT4-doc-end
    """
    pass

class UnreadableGPTDocumentation(Exception):
    """ 
    @GPT4-doc-begin
    raised when the documentation of a specified function cannot be read
    by GPT-4V, either because the documentation doesn't exist or the proper
    tags are not inside it.
    @GPT4-doc-end
    """
    pass


class CodeExecutionException(Exception):
    """
    @GPT4-doc-begin
    raised when the execution of the LLM-produced code results in an error,
    and stops execution.
    @GPT4-doc-end
    """
    pass
