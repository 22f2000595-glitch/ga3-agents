# Three-agent Render deployment

This bundle serves all three agents from one HTTPS origin and one Render Web Service:

- Invoice A2A: `/.well-known/agent-card.json`, `/a2a/message:send`, `/a2a/tasks...`
- Mailroom: `POST /` and `GET /healthz`
- Incident: `/v2/incidents...`

All three agents call Groq through its OpenAI-compatible chat-completions endpoint. Configure `GEMINI_API_KEY`, `GEMINI_BASE_URL`, and `GEMINI_MODEL`. Their task/evaluation state is stored in the shared Render Postgres database.

## Deploy

1. In the GitHub repository already connected to your invoice Render service, replace its root files with the contents of this folder. Keep the service name and existing `invoice-agent-db` Blueprint resource.
2. Commit and push. Render should redeploy the existing service from the same repository; do not create a second Render service or a second free Postgres database.
3. Enter your Groq API key as the value for `GEMINI_API_KEY` when prompted. The selected Groq model must be available to the Groq account and within its free-plan limits.
4. Wait for the existing service to become Live. The invoice Agent Card advertises the current Render hostname with `/a2a/`.
5. Open `https://<your-render-host>/.well-known/agent-card.json` to verify the invoice card. Use `https://<your-render-host>/a2a/` as the invoice A2A base URL. The same hostname serves mailroom at `/` and incident routes under `/v2/incidents`.

The free web plan sleeps while idle. The existing free Postgres database has a 1 GB limit and expires after 30 days, and free web compute may restart. The included PostgreSQL integration persists app state across normal requests and service restarts while the database exists. Use it only for synthetic grader data, not production records. [Render free plan details](https://render.com/docs/free).

## Local run

Requires Docker and a PostgreSQL `DATABASE_URL`, Node.js 22, Python dependencies from `requirements.txt`, and npm dependencies from `package.json`. Set `GEMINI_API_KEY`, `GEMINI_BASE_URL=https://api.groq.com/openai/v1`, and `GEMINI_MODEL=qwen/qwen3.8-27b`. Start the web app with `uvicorn invoice_app:app --host 0.0.0.0 --port 10000`; its startup hook starts the incident server on the loopback interface.
