# Three-agent suite: fresh repository deployment

This repository packages the invoice A2A agent, mailroom agent, and incident agent in one Render Web Service. It keeps each API contract on the same HTTPS origin:

- Invoice A2A: `/.well-known/agent-card.json`, `/a2a/message:send`, `/a2a/tasks...`
- Mailroom: `POST /`
- Incident: `/v2/incidents...`

All three use one OpenAI API key and the `gpt-4o-mini` chat-completions model. Task state uses the existing Render Postgres database named `invoice-agent-db`, so the Blueprint does not create another free database. This connection requires that database to exist in the same Render workspace.

## Create a new GitHub repository and deploy

1. Push these repository files to a new GitHub repository.
2. In Render, choose **New → Blueprint**, connect the new repository, and deploy `render.yaml`.
3. Enter your OpenAI API key for `OPENAI_API_KEY`. The API key needs available credits and usage quota; keep it in Render secrets, not GitHub.
4. The new service will have its own `onrender.com` hostname. The invoice Agent Card reads Render's runtime hostname automatically, so no hostname edit is needed.
5. After the service is Live, visit `https://<new-render-host>/.well-known/agent-card.json`. Submit `https://<new-render-host>/a2a/` for the invoice agent. The same host serves the mailroom at `/` and incident routes at `/v2/incidents`.

The Render Blueprint references the existing `invoice-agent-db` database rather than creating another one. If Render cannot resolve that database from the new Blueprint, create the Web Service and set `DATABASE_URL` to the existing database's internal connection string in Render's Environment settings.

Render's free Web Service may sleep while idle. Its free Postgres database has a 1 GB limit and expires 30 days after creation. This setup is for synthetic grader data, not production records.
