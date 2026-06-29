import os
from pathlib import Path
from fastapi import FastAPI, Depends
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from fastapi_clerk_auth import ClerkConfig, ClerkHTTPBearer, HTTPAuthorizationCredentials
from openai import OpenAI

app = FastAPI()

# Add CORS middleware (allows frontend to call backend)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Clerk authentication setup
clerk_guard = None


class Visit(BaseModel):
    patient_name: str
    date_of_visit: str
    notes: str


system_prompt = """
You are provided with notes written by a doctor from a patient's visit.
Your job is to summarize the visit for the doctor and provide an email.
Reply with exactly three sections with the headings:
### Summary of visit for the doctor's records
### Next steps for the doctor
### Draft of email to patient in patient-friendly language
"""


def get_clerk_guard():
    """Return the Clerk auth dependency, or a no-op if Clerk is not configured."""
    if clerk_guard is not None:
        return clerk_guard
    # No Clerk configured — return a callable that FastAPI's Depends can use that returns None
    return _noop_auth


async def _noop_auth():
    """No-op auth dependency: returns None when Clerk is not configured."""
    return None


@app.on_event("startup")
def init_clerk():
    import logging
    logger = logging.getLogger("uvicorn")

    jwks = os.getenv("CLERK_JWKS_URL")

    if not jwks:
        logger.error("🚨 CLERK_JWKS_URL environment variable is missing! Authentication will not work (disabled).")
        logger.error("💡 Make sure you loaded .env and passed -e CLERK_JWKS_URL to docker run.")
        return

    logger.info("✅ Clerk JWKS URL found, initializing auth...")
    global clerk_guard
    config = ClerkConfig(jwks_url=jwks)
    clerk_guard = ClerkHTTPBearer(config)
    logger.info("✅ Clerk authentication initialized successfully")
    

def user_prompt_for(visit: Visit) -> str:
    return f"""Create the summary, next steps and draft email for:
Patient Name: {visit.patient_name}
Date of Visit: {visit.date_of_visit}
Notes:
{visit.notes}"""


def build_llm_stream(visit: Visit):
    """Shared helper: streams the OpenAI completion for a visit."""
    client = OpenAI()

    user_prompt = user_prompt_for(visit)
    prompt = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    stream = client.chat.completions.create(
        model="gpt-5-nano",
        messages=prompt,
        stream=True,
    )

    def event_stream():
        for chunk in stream:
            text = chunk.choices[0].delta.content
            if text:
                lines = text.split("\n")
                for line in lines[:-1]:
                    yield f"data: {line}\n\n"
                    yield "data:  \n"
                yield f"data: {lines[-1]}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api")
def consultation_summary_index(
    visit: Visit,
    creds: HTTPAuthorizationCredentials | None = Depends(get_clerk_guard),
):
    """POST /api — used by the / (index) page."""
    return build_llm_stream(visit)


@app.post("/api/consultation")
def consultation_summary(
    visit: Visit,
    creds: HTTPAuthorizationCredentials | None = Depends(get_clerk_guard),
):
    """POST /api/consultation — used by the /product page."""
    return build_llm_stream(visit)


@app.get("/health")
def health_check():
    """Health check endpoint (used for local Docker; Lambda does not invoke it)"""
    return {"status": "healthy"}

# Serve static files (our Next.js export) - MUST BE LAST!
static_path = Path("static")
if static_path.exists():
    @app.get("/")
    async def serve_root():
        return FileResponse(static_path / "index.html")

    app.mount("/", StaticFiles(directory="static", html=True), name="static")