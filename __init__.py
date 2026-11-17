"""Hermes directory plugin entry point. OAuth is native-only."""


def register(ctx):
    def setup(parser):
        parser.add_argument("action", choices=["doctor"])
        parser.add_argument("--discovery-only", action="store_true",
                            help="Check directory plugin discovery without requiring runtime settings")

    def doctor(args):
        from .agentcore_core.doctor import run
        return run(ctx, args)

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
