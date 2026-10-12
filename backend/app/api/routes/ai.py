from fastapi import APIRouter, Depends, HTTPException
import anthropic
from app.core.config import settings
from app.core.deps import get_current_user
from app.core.ratelimit import rate_limit_user
from app.models.models import User
from app.schemas.schemas import AIChatRequest, AIChatResponse

router = APIRouter(prefix="/api/ai", tags=["ai"])

# Bound the input so a single request cannot run up an unbounded model bill. Generous enough for
# normal conversational use; over the cap → a clean 400 (never silently truncated). Tunable here.
MAX_AI_MESSAGES = 50
MAX_AI_TOTAL_CHARS = 20_000

SYSTEM_PROMPT = """You are an intelligent AI assistant for Clari5Pay, an enterprise Payment Service Provider (PSP) platform.

You help users — merchants, admins, and super admins — understand:
- Their transaction history, statuses, and volumes
- Deposit types: UPI, QR, IMPS, NEFT, RTGS, CASH
- The 2-step approval workflow: Merchant submits → Admin approves → Super Admin completes
- Risk analysis, risk scores, and fraud prevention
- Fee structures: pay-in fees, pay-out fees, settlement codes
- Balance enquiries, settlements, and withdrawals
- Platform features: integrations (WhatsApp, Telegram, Email), profile management

Always be helpful, precise, and professional. When discussing amounts, use Indian Rupee (INR) formatting.
Keep responses concise and actionable. If asked about specific transaction data you don't have access to, 
acknowledge that and guide the user to check the relevant section of the platform.

Platform roles:
- MERCHANT: Can submit deposits, withdrawals, settlements; view own transactions
- ADMIN: Can approve/reject pending transactions; manage merchants
- SUPER_ADMIN: Final approvals; manages admins and merchants; risk intelligence
"""


@router.post(
    "/chat",
    response_model=AIChatResponse,
    dependencies=[Depends(rate_limit_user(settings.RL_AI_PER_MIN, 60, "ai-chat"))],
)
async def ai_chat(
    request: AIChatRequest,
    current_user: User = Depends(get_current_user),
):
    if not settings.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="AI service not configured")

    # Bound the input before spending any model tokens on it.
    if len(request.messages) > MAX_AI_MESSAGES:
        raise HTTPException(status_code=400, detail="Conversation is too long. Please start a new chat.")
    total_chars = sum(len(m.content or "") for m in request.messages)
    if total_chars > MAX_AI_TOTAL_CHARS:
        raise HTTPException(status_code=400, detail="Message is too long. Please shorten it and try again.")

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    messages = [
        {"role": msg.role, "content": msg.content}
        for msg in request.messages
        if msg.role in ("user", "assistant")
    ]

    # Inject user context into first message if needed
    user_context = f"\n\n[User context: {current_user.name}, Role: {current_user.role}, Balance: INR {current_user.balance or 0:,.2f}]"
    if messages and messages[0]["role"] == "user":
        messages[0] = {
            "role": "user",
            "content": messages[0]["content"] + user_context,
        }

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=messages,
    )

    reply = response.content[0].text
    return AIChatResponse(reply=reply)
