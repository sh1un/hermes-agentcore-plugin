"""Hermes directory plugin entry point. No OAuth tools are registered."""


def register(ctx):
    def setup(parser):
        parser.add_argument("action", choices=["doctor"])

    def doctor(args):
        print("AgentCore foundation loaded. Live OAuth and Slack UI are not enabled.")
        print("Required: authenticated per-invocation identity and native Slack event bridge.")
        print("Do not use pre_gateway_dispatch as a post-authorization boundary.")

    ctx.register_cli_command(
        name="agentcore",
        help="Inspect the experimental AgentCore plugin",
        setup_fn=setup,
        handler_fn=doctor,
    )
