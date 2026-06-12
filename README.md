# LeadScraper

You describe your ideal lead. LeadScraper figures out where to look, searches across Google Maps, LinkedIn, Instagram, X, and a dozen other sources, scores every result, and hands you a clean Excel file ready to work with.

LeadScraper helps to make lead discovery smart and effortless.

---

## How to use it

1. Sign in with Google.
2. Type what you're looking for in the search box. Be as specific or as vague as you want — the AI reads your intent.
   - *"yoga studios in Pune and Mumbai, small local businesses only"*
   - *"financial advisors on Instagram who may need a website"*
   - *"IT companies in Bangalore with a rating above 4 and at least 50 reviews, I want to pitch corporate catering"*
3. Hit **Start**. The system builds a plan, picks the right sources, and begins searching.
4. Watch leads appear in the table along with logs of what;s happening in real time as they're found.
5. When it finishes, click **Download Excel**. The file is sorted by priority — work from the top.

You can stop a job at any point and still download what was found.

---

## What the priority colours mean

Every lead is scored 0–100 and placed into one of these tiers:

| Colour | Tier | What it means |
|---|---|---|
| Red | Hot | Very high opportunity — matches your criteria strongly, highly contactable |
| Orange | Warm | Good match — worth reaching out |
| Yellow | Medium | Partial match — lower confidence or missing contact info |
| Grey | Cold | Weak match — still logged, but de-prioritise |

When you don't specify any filter criteria, scoring defaults to a website-gap signal: businesses with no website score highest, on the assumption you're pitching one. When you do specify criteria (e.g. "rating above 4", "has email", "fewer than 50 reviews"), scoring switches to data richness — how contactable and verifiable the lead is — with no assumptions about what makes a good lead for your specific use case.

---

## What you can ask for

The system understands natural language criteria and converts them into filters automatically. A few examples of things you can express in your search:

- "who need a website" — filters for leads with no website found
- "with broken website" — filters for leads whose site is down or unreachable
- "no HTTPS" — filters for sites still running on HTTP
- "have a contact email" — filters for leads with an email address in the listing
- "rating above 4" — numeric filter on Google Maps rating
- "fewer than 50 reviews" — size/maturity signal
- "hot leads only" — limits results to the top scoring tier

Mix and match. They all work together.

---

## How it works

When you submit a query, the backend does the following in order:

**1. Plan generation**
An LLM (Llama 3.3 70B on Groq) reads your query and produces a structured scraping plan: which cities to search, what queries to run, which sources to use, and what filters to apply. It decides whether your target is a physical business (uses Google Maps), an online presence (uses LinkedIn, Instagram, X, etc.), or both.

**2. Scraping**
For physical leads, a headless Chromium browser navigates Google Maps neighbourhood by neighbourhood and extracts business listings. For online/social leads, the system runs web searches with platform-specific queries rather than using blocked `site:` restrictions. All source types fall back gracefully from Google search to DuckDuckGo if rate-limited.

**3. Enrichment**
Each lead's website (if any) is checked: is it reachable? Does it have HTTPS? Is it mobile-friendly? Social profile URLs are normalised — an Instagram URL in the website field gets moved to the social handle field, not counted as a website.

**4. Filtering and scoring**
Leads that don't match your filter criteria are dropped before being saved. Leads that pass are scored by data richness (contactability) or by website-gap signal depending on whether you specified criteria. Everything is written to Firestore in real time so the frontend updates as results come in.

**5. Export**
When the job finishes, an Excel file is generated with all leads sorted by score, with columns for name, phone, email, website, Instagram, category, city, area, rating, review count, priority, and lead type.

---

## Tech stack

| Layer | Technology |
|---|---|
| Frontend | Next.js, deployed on Vercel |
| Backend | FastAPI (Python), deployed on Render |
| AI / LLM | Groq (Llama 3.3 70B) orchestrated with LangChain |
| Browser automation | Playwright (Chromium headless) |
| Web search fallback | DuckDuckGo HTML + ddgs library |
| Authentication | Firebase Auth (Google sign-in) |
| Database | Firestore (real-time lead sync) |
| File storage | Firebase Storage |
| Scoring / filtering | Custom predicate evaluator (no ML) |

---

## Running locally

```bash
# Backend
cd backend
pip install -r requirements.txt
uvicorn main:app --reload

# Frontend
cd frontend
npm install
npm run dev
```

You'll need a `.env` file in `/backend` with `GROQ_API_KEY` and `FIREBASE_SERVICE_ACCOUNT`.

---

v0.4.4
