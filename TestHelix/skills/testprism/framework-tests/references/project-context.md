# Repository context, on demand

Use this index when an unfamiliar project's conventions block an executable test. Public task text and this checkout outrank these orientation notes. Current upstream documentation may describe a newer API, layout or runner. Do not install or upgrade dependencies just to match the notes.

Resolve one concrete uncertainty: identify the relevant package from local metadata, inspect its configured runner and one neighboring test, and verify that the selected runtime or built target executes the intended checkout. Record only the chosen runner, setup requirement and unresolved semantic assumption. Then execute a small supported operation. Reuse that evidence instead of repeatedly surveying the tree.

Choose one reference by the code involved, not by the benchmark source label. A repository can cross these families; read a second only for an observed unresolved dependency.

| Work involved | Reference | Representative projects |
| --- | --- | --- |
| Labeled arrays, lazy execution, statistics | `references/project-arrays.md` | xarray, pandas, sparse, Dask, opt_einsum, scikit-learn, statsmodels, ArviZ, Ibis |
| Symbolic math, units, simulation, model backends | `references/project-scientific.md` | SymPy, Astropy, Pint, pint-pandas, ucumvert, OpenFF, Cirq, PennyLane, Pyomo, Flax, Keras, Transformers, particle-swarm-optimization, Spinning Up, pyknotid |
| Rendering, components, browser state | `references/project-rendering.md` | Matplotlib, p5.js, Svelte, react-pdf, Material UI, Calypso, Proton webclients, Element Web, NodeBB, Expensify App, Vue/Vuex registration example |
| Parsing, static analysis, document processing | `references/project-languages.md` | pytest, mypy, pylint, wemake-python-styleguide, Sphinx, Lark, pymarkdown, Babel, price-parser, edtf-validate, dataspec, wcmatch |
| Framework setup, asynchronous clients, protocols | `references/project-services.md` | Django, aiogram, python-telegram-bot, kirara-ai, Open Library, kafka-python, oauthlib, pycoin, python-ecdsa, fakenewsredis, zulip-terminal, Bottle |
| CLI tools, servers, build systems | `references/project-systems.md` | Ansible, AWS CLI, charmcraft, jc, xonsh, yt-dlp, Serverless, Vuls, Flipt, Teleport, Trino, BusTub, DoomGeneric, STP, MdfLib |

These lists provide navigation, not an assertion that every project uses the same runner or supports every operation. Unlisted projects use the same local discovery method. Individual challenge tasks with no identified upstream repository must use their own public files; do not infer a framework from their dataset name.
