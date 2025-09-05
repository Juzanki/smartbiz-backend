Write-Host "`nðŸ› ï¸ Kukagua kama .env.production ipo..." -ForegroundColor Cyan

$envPath = Join-Path $PSScriptRoot ".env.production"

if (!(Test-Path $envPath)) {
    Write-Host "âš ï¸  Haipo. Tunaiunda sasa hivi..." -ForegroundColor Yellow

    @"
# ======================= ðŸŒ ENVIRONMENT CONFIG ============================
ENVIRONMENT=production
DEBUG=False
ACTIVE_DB=railway

# ======================= ðŸ›¢ DATABASE CONFIG ==============================
# Railway (Production)
RAILWAY_DATABASE_URL=postgresql://postgres:QHWefIcrwLtfnkVNxpHdzvTnzGzsFwjo@switchback.proxy.rlwy.net:17277/railway

# Local Development
LOCAL_DB_USER=postgres
LOCAL_DB_PASSWORD=SmartBiz2025
LOCAL_DB_HOST=localhost
LOCAL_DB_PORT=5432
LOCAL_DB_NAME=smartbiz_db
LOCAL_DATABASE_URL=postgresql+psycopg2://postgres:SmartBiz2025@127.0.0.1:5432/smartbiz_db

# Default fallback (Railway)
DATABASE_URL=postgresql://postgres:QHWefIcrwLtfnkVNxpHdzvTnzGzsFwjo@switchback.proxy.rlwy.net:17277/railway

# ======================= ðŸ” SECURITY CONFIG ===============================
SECRET_KEY=pK179z2QQzIyIVge415C0e7kvdIX6p4y9d7_AQ1HTcU_hh35U4dO5guT0eKTSgj5IunErK1byXqUz5O5ysh6cQ
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=60

# ======================= ðŸŒ APP URL CONFIG ===============================
RAILWAY_PUBLIC_URL=https://smartbiz-assistance-production.up.railway.app
NETLIFY_PUBLIC_URL=https://sprightly-naiad-bcfd2a.netlify.app
VITE_API_URL=https://smartbiz-assistance-production.up.railway.app
SMARTINJECT_URL=http://127.0.0.1:8010
SMARTINJECT_SECRET=732b64aa0c47f259f92aabf8e3fd19a9d3b3b47b

# ======================= ðŸ¤– AI & INTEGRATIONS =============================
OPENAI_API_KEY=sk-proj-CxzpN7aEVVhQwljHtFz0I3mjZrruMsuZlzcf1QfmeNkNbyXk
OPENAI_MODEL=gpt-3.5-turbo
HUGGINGFACE_API_TOKEN=$env:HUGGINGFACE_TOKEN
PIXELAI_API_KEY=gy8melZO6874VF9jFsSH7BjiliGgyBR2B7lL9tj37dlSHTB1lsT4KZz8
DEEPSEEK_API_KEY=$env:OPENAI_API_KEY

# ======================= ðŸ’³ PAYMENTS / WALLET =============================
PESAPAL_CONSUMER_KEY=OZo36FksIOm5WYc5J5LBJUDqGkUKIH8V
PESAPAL_CONSUMER_SECRET=q0dSa2eRoE3XnU4eL5ypQEr99ehTzP5C
PESAPAL_CALLBACK_URL=https://smartbiz.live/wallet/pesapal/callback

# ======================= ðŸ“„ PDF GENERATION ================================
PDF_GENERATOR_API_KEY=025974ef7454ffc164919a64edc3058c0a5e1728e1ebce7bb5550cac921ef654
PDF_GENERATOR_SECRET_KEY=9f9f64b537968833b0c3aeae4d48021aacee73a0339cd7703603a00a1ac3e96d

# ======================= ðŸ—º MAPS & GEOLOCATION ============================
GEOAPIFY_API_KEY=87e86db74158410da801b7a41ceb99c1

# ======================= â˜ CLOUD STORAGE ================================
CLOUDINARY_API_KEY=642978151613922
CLOUDINARY_API_SECRET=NsLD3sMgRku8R1mMJWuXg8mmb8g

# ======================= ðŸª™ CRYPTO & EXCHANGE ============================
COINBASE_API_KEY=b8aa7788-19ec-4af8-b837-3060b54b2769
EXCHANGE_API_KEY=85eba98037a0c634b2762592

# ======================= ðŸ“¸ IMAGES / UNSPLASH =============================
UNSPLASH_ACCESS_KEY=x0nJvpPDzN_lXyJMwI3iKLnMCxltyV77GBqFDhrXTBY
UNSPLASH_SECRET_KEY=o2oQNsdfvzTEkq6Dle40V_qSCnQc-HvnIGg_6COfETE

# ======================= ðŸ“¢ TELEGRAM / NOTIFICATIONS =====================
TELEGRAM_BOT_TOKEN=7085104120:AAHhifiBesBbRyJ2iFT-TwOB8X2VgWXfic
WEBHOOK_URL=https://smartbiz.live/telegram/webhook

# ======================= ðŸ“ž TWILIO / SMS ================================
TWILIO_SECRET=q7R0gyaY5WCvcERyga0Ljh6RCvuN8c6N

# ======================= ðŸ›¡ SECURITY ================================
SNYK_API_KEY=0055ed14-0945-4604-8c9f-b469929dad41

# ======================= ðŸŒŸ FRONTEND VITE CONFIG ==========================
VITE_ENVIRONMENT=production
VITE_APP_NAME=SmartBiz Assistance
VITE_APP_VERSION=1.0.0

# Feature Toggles
VITE_FEATURE_CHAT=true
VITE_FEATURE_LIVESTREAM=true
VITE_FEATURE_AI_ASSISTANT=true
"@ | Out-File -Encoding utf8 $envPath

    Write-Host "âœ… .env.production imeandikwa vizuri!" -ForegroundColor Green
} else {
    Write-Host "âœ… .env.production tayari ipo. Hatukuandika tena." -ForegroundColor Cyan
}


