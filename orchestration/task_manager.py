# orchestration/task_manager.py
import threading
import os
from datetime import datetime
from dotenv import load_dotenv
from llm import NativeLLM
from .active_task import ActiveTask

load_dotenv()


class TaskManager:
    def __init__(self):
        self.tasks = {}
        self.director_llm = NativeLLM(
            api_key=os.getenv("deepseek"),
            temperature=0.7
        )

    def start_mission(self, mission: str, agents: list) -> str:
        task_id = str(len(self.tasks) + 1)
        new_task = ActiveTask(task_id, mission, agents, self.director_llm, task_manager=self)
        self.tasks[task_id] = new_task
        new_task.start()
        return task_id

    def list_tasks(self):
        return list(self.tasks.values())

    def get_task(self, tid: str):
        return self.tasks.get(tid)

    def intervene(self, tid: str, instruction: str) -> bool:
        task = self.get_task(tid)
        if task:
            task.intervene(instruction)
            return True
        return False

    def trigger_dream_state(self) -> None:
        active = [t for t in self.tasks.values() if t.status not in ("COMPLETED", "AWAITING_OVERLORD")]
        if active:
            return
        threading.Thread(target=self._dream_state_worker, daemon=True).start()

    def _dream_state_worker(self) -> None:
        try:
            from empire_tools import library_collection

            all_docs = library_collection.get(include=["documents", "metadatas", "ids"])
            if not all_docs or not all_docs.get("documents"):
                return

            docs      = all_docs["documents"]
            metadatas = all_docs["metadatas"]
            ids       = all_docs["ids"]

            report_ids   = []
            lesson_texts = []
            for doc, meta, doc_id in zip(docs, metadatas, ids):
                if meta.get("type") == "intelligence_report":
                    report_ids.append(doc_id)
                else:
                    lesson_texts.append(doc[:600])

            if not lesson_texts:
                return

            consolidation_prompt = (
                f"You are the Imperial Archivist. Consolidate {len(lesson_texts)} knowledge fragments "
                f"into a Master Architecture File.\n\n"
                f"FRAGMENTS:\n"
                + "\n---\n".join(lesson_texts[:30])
                + "\n\nINSTRUCTIONS:\n"
                "1. Discard: outdated facts, contradictions, vague generalities.\n"
                "2. Merge: repeated patterns into authoritative rules.\n"
                "3. Output: Architecture Patterns | Known Bugs & Fixes | API Contracts | "
                "Deployment Rules | Anti-Patterns. Be specific. Max 2000 words."
            )

            master_doc = self.director_llm.call(
                messages=[{"role": "user", "content": consolidation_prompt}]
            )

            if report_ids:
                library_collection.delete(ids=report_ids[:50])

            master_id = f"MASTER_DOC_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            library_collection.upsert(
                documents=[master_doc],
                ids=[master_id],
                metadatas=[{
                    "type":       "MASTER_DOC",
                    "concept":    "Master Architecture File",
                    "created_at": datetime.now().isoformat(),
                    "trust_score": 1.0
                }]
            )

            dream_path = os.path.join(
                "ai_civilization", "dream_state",
                f"master_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
            )
            os.makedirs(os.path.dirname(dream_path), exist_ok=True)
            with open(dream_path, "w", encoding="utf-8") as f:
                f.write(f"# Master Architecture File\n_Consolidated: {datetime.now().isoformat()}_\n\n")
                f.write(f"_Merged {len(lesson_texts)} fragments. Deleted {len(report_ids)} stale reports._\n\n")
                f.write(master_doc)

        except Exception:
            pass
