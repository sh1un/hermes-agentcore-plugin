"""Hermes directory plugin entry point. OAuth is native-only."""


def register(ctx):
    def setup(parser):
        parser.add_argument("action", choices=["doctor"])

    def doctor(args):
        print("AgentCore plugin: native Cognito login, Consent portal entry and Gateway Jira reads.")
        print("Portal mode requires mode=portal. Legacy direct configurations remain supported.")
        print("Requires register_platform_handler and task-local Slack identity context.")
        print("Configure plugins.entries.agentcore.settings.config_file to enable.")

    ctx.register_cli_command(
        name="agentcore",
        help="Inspect the experimental AgentCore plugin",
        setup_fn=setup,
        handler_fn=doctor,
    )
    config_file = ctx.get_config("config_file")
    if config_file:
        from .agentcore_core.host import enable
        enable(ctx, config_file)
