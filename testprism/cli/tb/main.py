import dotenv
from typer import Typer

from testprism.cli.tb.runs import create, runs_app
from testprism.cli.tb.testbench import testbench_app, validate_tasks

dotenv.load_dotenv()

app = Typer(no_args_is_help=True)
app.add_typer(
    testbench_app,
    name="testbench",
    help="TestBench commands (test-generation evaluation).",
)
app.add_typer(runs_app, name="runs", help="Manage runs.")

app.command(name="run", help="Run the harness (alias for `tb runs create`)")(create)
app.command(
    name="validate-tasks",
    help="Validate task legality (alias for `tb testbench validate`).",
)(validate_tasks)

if __name__ == "__main__":
    app()
