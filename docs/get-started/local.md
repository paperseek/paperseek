# Run your first search locally

Install PaperSeek on your own machine, connect a model, and turn a question into a reading list.

Prefer to skip installation? [Run your first search online](online.md) instead.

## 1. Install

Use **Python 3.12 or newer** and an isolated environment.

::: code-group
~~~bash [macOS / Linux]
python3 -m venv .venv
source .venv/bin/activate
python -m pip install "paperseek==1.0.0b4"
~~~
~~~powershell [Windows PowerShell]
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install "paperseek==1.0.0b4"
~~~
:::

For a 1.0.0-beta4 source checkout, use `python -m pip install ./backend` instead. After changing frontend source, follow the [contributor build instructions](../developers/contributing.md).

## 2. Start

~~~bash
paperseek serve
~~~

Open **[http://127.0.0.1:8765](http://127.0.0.1:8765)**. Keep the terminal running while you use the local app.

The package includes the web interface; Node.js is not required to run it.

## 3. Connect a model

Open **Settings → Model APIs** at the bottom of the sidebar and choose **Add connection**. Select **LLM**, a provider and a model, then enter your key. For a custom service, check the base URL and protocol as well. You can use **Test connection** to check the model draft, then click **Save** to keep it. Under **Defaults**, select the saved connection for **Search and scoring**. Return through **Search** in the sidebar.

OpenAI Chat Completions, OpenAI Responses, and Anthropic Messages are distinct protocols. A model name alone does not identify the right one.

Model draft testing does not save the connection. Data-source testing has different save behavior, described below. To supply deployment defaults instead, see [configuration](../reference/configuration.md).

## 4. Ask a question

> How does open-source software obtain funding for long-term maintenance?

If you have an OpenAlex key, open **Settings → Data sources → OpenAlex → Configure**, enter it, and click **Save**. **Test connection** in this data-source dialog also saves the current key and options before testing; a failed test or subsequently cancelling the dialog does not undo the save.

Return to **Search** in the sidebar and choose **OpenAlex**. Only restrict the research field under **More options** when needed. Leave the budget on **Auto** for a first run, or choose a budget to match the breadth of your question.

Select **Start search**, or press **Ctrl+Enter / ⌘+Enter**. Follow the stages while PaperSeek searches and ranks.

## 5. Read the results

Open **Results**, inspect abstracts and relevance explanations, and export CSV, JSON, BibTeX, or RIS. Community saves history locally.

Reopen a run from **Search history** in the sidebar, or click **All** beside that heading for the full list. Use **Citation evidence** to explore available citation data, or select papers for an [experimental review](../guide/reviews.md). Open [Bibliometrics](../guide/bibliometrics.md) from the sidebar to analyze the full retrieved candidate corpus.

## If something fails

Start with [troubleshooting](../reference/troubleshooting.md). For a local setup check:

~~~bash
paperseek doctor --json
~~~

A valid configuration does not guarantee available credit or uninterrupted upstream service throughout a long search.

## What next?

- [Self-hosting](../self-hosting/index.md): Docker deployment, data directories, network access, and backups.
- [Search the literature](../guide/search.md): budgets, field filters, stopping and reconnecting.
