# Agent instructions

## About this project
This repository contains a small numeric utility library. It is maintained by a team that values clarity, correctness and consistency above cleverness. Every contributor, human or AI, is expected to read this whole document before making any change, and to follow it exactly.

## General principles
- Always think carefully before you act. Consider the problem from several angles before choosing a solution.
- Prefer simple, readable code over clever code. Code is read far more often than it is written.
- Be consistent with the existing style of the codebase. When in doubt, look at nearby code and do what it does.
- Never introduce a change you cannot explain. Every line you write should have a clear purpose.
- Make the smallest change that fully solves the problem. Avoid drive-by refactors.
- Do not add new dependencies. This project uses the Python standard library only.
- Leave the code better than you found it, but only within the scope of the task.
- When something is ambiguous, state your assumption explicitly in your final message.

## Python style
- Target Python 3. Use features available in currently supported Python versions.
- Follow PEP 8 for naming, spacing and layout. Use four spaces for indentation, never tabs.
- Use descriptive names. Avoid single-letter names except for short loop indices or well-known math variables.
- Write docstrings for public functions. A docstring should say what the function returns and any important edge cases.
- Keep functions short and focused. A function should do one thing.
- Prefer early returns over deeply nested conditionals.
- Avoid mutable default arguments.
- Use list comprehensions when they make code clearer, and plain loops when they do not.
- Do not leave commented-out code behind.
- Do not use print statements for debugging in committed code.
- Type hints are welcome but not required. Do not add them to code you are not otherwise changing.

## Numbers and correctness
- Be careful with integer versus float division. Know which one you need.
- Consider empty inputs, single-element inputs and very large inputs.
- Do not silently swallow errors. If an input is invalid, raise a clear exception or document the behavior.
- Avoid floating point comparisons with `==` in new tests unless the values are exact.
- Off-by-one errors are a common source of bugs. Check loop bounds and slices carefully.

## Testing
- Tests live in the `tests/` directory and use the standard `unittest` module.
- Run the full test suite with `python -m unittest discover -s tests` before you finish.
- Do not modify existing tests to make them pass. Fix the code under test instead.
- If you add behavior, add a test for it. If you fix a bug, consider adding a regression test.
- Tests must be deterministic. Do not depend on time, randomness or network access.
- Keep test names descriptive, so a failing test explains itself.

## Workflow
- Read the relevant code and tests before changing anything.
- Reproduce the problem first, for example by running the failing test.
- Make the change, then run the tests again.
- Review your own diff before you report that you are done.
- In your final message, summarize what you changed and why, in a few sentences.
- Mention any follow-up work you noticed but did not do.

## Communication
- Be concise. Do not repeat the task back.
- Use plain language. Avoid jargon when a simpler word works.
- If you are unsure, say so rather than guessing.
- Do not claim that tests pass unless you ran them and saw them pass.

## Things to avoid
- Do not reformat files you did not otherwise change.
- Do not rename public functions.
- Do not change the behavior of functions that are not part of the task.
- Do not add logging, configuration or command-line interfaces.
- Do not create new files unless they are needed for the task.
- Do not commit generated files or caches.

## Final checklist
1. The change is minimal and solves the stated problem.
2. All tests pass when run with `python -m unittest discover -s tests`.
3. No tests were weakened or deleted.
4. The code follows the style rules above.
5. Your final message explains the change briefly.
