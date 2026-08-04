# PROJECT HANDBOOK: HVT CV Manager

## 1. Executive Summary
HVT CV Manager is an AI‑powered career automation platform that helps professionals build tailored, ATS‑optimised CVs and manage their job search from a single dashboard. By combining an intuitive CV builder, a smart application tracker, and an AI orchestration engine, the platform transforms the job search process from a chaotic, manual effort into a streamlined, data‑driven workflow.

## 2. Project Identity

| Aspect              | Detail                                                                                |
|---------------------|---------------------------------------------------------------------------------------|
| Application Name    | HVT CV Manager                                                                        |
| Tagline             | Build, track, and optimise your job search — with AI.                                 |
| Purpose             | AI‑powered CV generation, job application tracking, and career automation.            |
| Website             | `https://www.franciscodes.com/cv`                                                     |
| Target Audience     | Job seekers, career changers, and professionals who want to stand out in a competitive market. |

## 3. Value Proposition

**For Job Seekers**  
- Write tailored, ATS‑optimised CVs in minutes.  
- Build multiple CVs for different roles (e.g., Data Analyst CV, Software Engineer CV).  
- Get AI‑driven feedback on CV content, structure, and impact.  
- Generate custom cover letters for each application.

**For Career Professionals**  
- Track applications with smart reminders and status updates.  
- Organise applications with custom tags (e.g., Remote, Hybrid, Urgent).  
- Gain insights into application performance: response rates, success rates, and conversion funnels.  
- Automatically scrape and evaluate job postings.

## 4. Core Features

### 4.1 CV Builder
- **Nested Sections** – Education, Experience, Projects, Skills, Languages, Achievements.
- **Customisable Order** – Drag‑and‑drop or arrow‑based reordering of sections and items.
- **Markdown Support** – Add bullet points, bold, italic, and links (rendered in UI and PDF).
- **Multiple CVs** – Create separate CVs for different roles.
- **Duplicate CV** – One‑click duplication to create new versions.
- **PDF Export** – Download a polished PDF with the same section order as the frontend.
- **CV Title** – Optional title to distinguish CVs (e.g., “Data Analyst CV”).
- **Currently Working Toggle** – Mark an experience as current (displays “Present” in UI and PDF).

### 4.2 Job Application Tracker
- **Status Pipeline** – Saved → Applied → Follow‑up → Interviewing → Offered → Rejected.
- **Timeline View** – Visualise each application’s journey.
- **Smart Reminders** – Highlight applications where `date_applied > 3 weeks` and status is “Applied”.
- **One‑Click Follow‑up** – Mark an application as “Follow‑up” directly from the dashboard.
- **Deadline Tracking** – Optional deadline dates.
- **Notes** – Custom notes per application.
- **Tags** – Add custom tags (e.g., “Remote”, “Hybrid”, “Urgent”) and filter by them.

### 4.3 Analytics & Insights
- Response Rate, Success Rate, Average Days to Interview.
- Monthly application activity chart.
- Status Distribution (pie / bar charts).
- CV Performance Table (which CVs get the most interviews / offers).
- Tag Frequency analysis.
- Conversion Funnel: Applied → Follow‑up → Interviewing → Offered → Rejected.
- Drop‑off Rates between stages.
- Highest Stage Reached (so rejections don’t skew history).

### 4.4 User Management
- **JWT Authentication** – Custom HVT backend.
- **Guest Data** – Try without login; migrate data on registration.
- **Session Management** – Refresh tokens with configurable expiry.

### 4.5 AI Features (Planned / In Progress)
- **CV Analysis** – AI feedback on content, structure, and impact.
- **Cover Letter Generator** – Custom cover letter from CV + job description.
- **Job Matching** – Highlight best‑matched skills and gaps.
- **Interview Question Generator** – Potential questions based on CV and role.

## 5. Brand Guidelines

### 5.1 Tone & Voice
- **Professional** – Helpful, data‑driven, user‑centric.
- **Modern** – Clean, minimal, focused on experience.
- **Empowering** – Give users control of their career journey.

### 5.2 Brand Standards
- Use exact company names, job titles, and contact information.
- No fictitious data in production.
- All content must be accurate and up‑to‑date.

### 5.3 Visual Identity
| Element       | Detail                                                       |
|---------------|--------------------------------------------------------------|
| Theme         | Dark theme (slate / gray) with blue accents                  |
| Primary Color | `#915EFF` (purple) or `#3B82F6` (blue) – choose one        |
| Typography    | Sans‑serif (e.g., Inter, Helvetica)                          |
| Icons         | Consistent set (e.g., Lucide, Tabler Icons)                  |
| Buttons       | Rounded, with hover and transition effects                   |

### 5.4 UX Principles
- Clean & Uncluttered – Avoid unnecessary complexity.
- Intuitive Navigation – Everything within two clicks.
- Real‑time Feedback – Toast notifications instead of `alert()`.
- Mobile‑first – Works well on all screen sizes.

## 6. Technical Architecture

### 6.1 Frontend
- **Framework** – React (Vite)
- **State Management** – React Context API (`useHVT`, `useCVData`)
- **Styling** – Tailwind CSS
- **Routing** – React Router DOM
- **Charts** – Recharts
- **Markdown** – react‑markdown + remark‑gfm
- **PDF Export** – Backend‑generated PDF via pdfkit + markdown (Django)

### 6.2 Backend
- **Framework** – Django (Django REST Framework)
- **Database** – PostgreSQL (application data), SQLite (local dev / mission state)
- **Authentication** – Custom JWT (HVT)
- **API** – DRF with paginated responses
- **PDF Generation** – pdfkit + wkhtmltopdf
- **Markdown Rendering** – Python `markdown` library
- **AI Orchestration** – FastAPI (Empire Orchestrator – multi‑agent system)

### 6.3 Infrastructure
- **Containerisation** – Docker (Docker Compose)
- **Reverse Proxy** – Nginx Proxy Manager
- **VPS** – Self‑managed VPS (Ubuntu)
- **Hosting** – `auth.franciscodes.com`, `api.franciscodes.com`, `cv.franciscodes.com`
- **Redis** – For caching and throttling
- **Email** – SendGrid (or similar) for notifications

### 6.4 Data Flow
Frontend (React) → Backend (Django / DRF) → Database (PostgreSQL)
↓ ↓
AI Orchestrator (FastAPI / Empire Orchestrator) ← Mission State (SQLite)
↓
AI Agents (CrewAI) → Response (Streamed or JSON)

text

## 7. Development Roadmap

### ✅ Phase 1 – MVP (Completed)
- CV Builder (nested sections, markdown, reordering)
- PDF Export
- JWT Authentication
- Job Application Tracker (status pipeline, tags, deadline)
- Guest Data Support
- Insights Dashboard (stats, charts, CV performance)

### 🚧 Phase 2 – AI Integration (In Progress)
- CV Analysis (AI feedback on content, structure, impact)
- Cover Letter Generator
- Job Matching
- AI‑powered CV improvements

### 🔮 Phase 3 – Automation & Scale
- Job Scraping (pull listings from major platforms)
- Weekly Insights Emails (automated reports)
- Calendar Sync (Google / Outlook)
- Bulk Actions (delete, update statuses)
- Public CV Link (shareable profiles)

## 8. Deployment & Maintenance

### 8.1 Deployment Checklist
1. **Backend** – Deploy on VPS using Docker Compose.
2. **Frontend** – Deploy on VPS or Vercel/Netlify.
3. **Database** – PostgreSQL (production) with daily backups.
4. **SSL** – Nginx Proxy Manager (Let’s Encrypt).
5. **Monitoring** – Health checks (`/healthz`, `/readyz`).
6. **Logging** – Centralised logging (ELK or Docker logs).

### 8.2 Environment Variables

**Backend (Django)**  
HVT_BASE_URL=https://auth.franciscodes.com/api/v1
HVT_API_KEY=...
DEBUG=False
DATABASE_URL=postgres://...
REDIS_URL=redis://:password@redis:6379/1

text

**Frontend (React)**  
VITE_API_BASE=https://api.franciscodes.com
VITE_AUTH_BASE=https://auth.franciscodes.com

text

## 9. Future Vision

HVT CV Manager aims to become the **all‑in‑one career cockpit** for professionals, combining:
- CV creation & optimisation (powered by AI)
- Application tracking (with smart reminders)
- Job market intelligence (data‑driven insights)
- Career coaching (personalised recommendations)

**The ultimate goal:** Turn the job search from a guessing game into a data‑driven strategy.

## 10. Appendix

### A. Technologies Used

| Layer          | Technologies                                      |
|----------------|---------------------------------------------------|
| Frontend       | React, Tailwind CSS, Recharts, react‑markdown     |
| Backend        | Django, DRF, PostgreSQL, Redis, Celery (optional) |
| AI             | FastAPI, CrewAI, Empire Orchestrator             |
| Infrastructure | Docker, Nginx, Let’s Encrypt, VPS                |
