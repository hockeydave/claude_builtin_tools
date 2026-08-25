## Claude built-in Tools
1.  Server side
1.1 search
1.1 code execution

Two things to notice:

1. There's no agent loop here. We don't switch on stop_reason. We don't push tool results back. Anthropic runs the tool server-side, and the response already contains the result.
1. The response has new block types. A server_tool_use block for the tool call, a code execution tool result block for the output, plus the regular text blocks.

#Server tools — web search, code execution, web fetch — are declared in your tools array. Anthropic runs them.
You get the result in the same response, with no agent loop required. Look for server_tool_use and tool result blocks alongside the regular text blocks.
#Client tools like memory and bash run where your code runs, but the SDK ships the schema and a runner for you.
The "hosted by Anthropic" idea scales all the way up: managed agents apply it to the entire agent, not just one tool.

# To run
python builtin-tools.py
