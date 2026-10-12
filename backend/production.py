"""Deterministic demo project-bible and screenplay/storyboard slice."""
from __future__ import annotations

import json
from uuid import uuid4
from backend.database import Database
from backend.repository import utc_now
from backend.schemas import ShotUpdate


def _id(prefix: str) -> str: return f"{prefix}_{uuid4().hex}"

class ProductionRepository:
    def __init__(self, database: Database) -> None: self.database = database

    def _latest_analysis(self, connection, project_id: str):
        return connection.execute("SELECT id,result_json FROM story_analysis WHERE project_id=? ORDER BY created_at DESC LIMIT 1", (project_id,)).fetchone()

    def generate_bible(self, project_id: str) -> dict | None:
        now = utc_now()
        with self.database.connect() as c:
            project = c.execute("SELECT visual_style,creative_intent,audience FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project: return None
            analysis = self._latest_analysis(c, project_id)
            if not analysis: raise ValueError("analysis required")
            data = json.loads(analysis["result_json"])
            version = c.execute("SELECT COALESCE(MAX(version),0)+1 FROM bible_versions WHERE project_id=?", (project_id,)).fetchone()[0]
            bible_id = _id("bible")
            style = {"visual_style": project["visual_style"], "creative_intent": project["creative_intent"],
                     "audience": project["audience"], "notice": "确定性 demo/local 草稿，不是 AI 生成结果。"}
            c.execute("INSERT INTO bible_versions (id,project_id,version,status,source_analysis_id,style_json,demo,generator,created_at,updated_at) VALUES (?,?,?,'draft',?,?,1,'demo-deterministic-v1',?,?)",
                      (bible_id, project_id, version, analysis["id"], json.dumps(style, ensure_ascii=False), now, now))
            entities = []
            for pos, item in enumerate(data.get("characters", []), 1): entities.append(("character", pos, item.get("name") or f"角色 {pos}", item))
            for pos, item in enumerate(data.get("scenes", []), 1): entities.append(("location", pos, item.get("title") or f"地点 {pos}", item))
            # Analysis has no prop field; don't invent asserted props.
            for kind, pos, name, item in entities:
                c.execute("INSERT INTO bible_entities (id,bible_version_id,kind,position,name,data_json,status) VALUES (?,?,?,?,?,?,'draft')",
                          (_id(kind), bible_id, kind, pos, name, json.dumps(item, ensure_ascii=False)))
        return self.get_bible(project_id, version)

    def get_bible(self, project_id: str, version: int | None = None) -> dict | None:
        with self.database.connect() as c:
            if version is None:
                row = c.execute("SELECT * FROM bible_versions WHERE project_id=? ORDER BY version DESC LIMIT 1", (project_id,)).fetchone()
            else: row = c.execute("SELECT * FROM bible_versions WHERE project_id=? AND version=?", (project_id, version)).fetchone()
            if not row: return None
            result = dict(row); result["style"] = json.loads(result.pop("style_json")); result["demo"] = bool(result["demo"])
            rows = c.execute("SELECT id,kind,position,name,data_json,status FROM bible_entities WHERE bible_version_id=? ORDER BY kind,position", (result["id"],)).fetchall()
        for key in ("characters", "locations", "props"): result[key] = []
        plural = {"character":"characters", "location":"locations", "prop":"props"}
        for item in rows:
            value = dict(item); value["data"] = json.loads(value.pop("data_json")); result[plural[value["kind"]]].append(value)
        return result

    def approve_bible(self, project_id: str, version: int) -> dict | None:
        now = utc_now()
        with self.database.connect() as c:
            row = c.execute("SELECT id FROM bible_versions WHERE project_id=? AND version=?", (project_id, version)).fetchone()
            if not row: return None
            c.execute("UPDATE bible_versions SET status='approved',approved_at=?,updated_at=? WHERE id=?", (now,now,row["id"]))
            c.execute("UPDATE bible_entities SET status='approved' WHERE bible_version_id=?", (row["id"],))
            c.execute("UPDATE workflow_stages SET status='completed',updated_at=? WHERE project_id=? AND stage_key='project_bible'", (now,project_id))
        return self.get_bible(project_id, version)

    def generate_script(self, project_id: str) -> dict | None:
        now = utc_now()
        with self.database.connect() as c:
            project = c.execute("SELECT target_duration_sec,episode_strategy FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project: return None
            analysis = self._latest_analysis(c, project_id)
            if not analysis: raise ValueError("analysis required")
            data = json.loads(analysis["result_json"]); target = int(project["target_duration_sec"])
            version = c.execute("SELECT COALESCE(MAX(version),0)+1 FROM episodes WHERE project_id=?", (project_id,)).fetchone()[0]
            episode_id = _id("ep"); synopsis = data.get("summary", "")
            c.execute("INSERT INTO episodes (id,project_id,version,position,title,synopsis,status,source_analysis_id,demo,created_at,updated_at) VALUES (?,?,?,1,?,?,'draft',?,1,?,?)",
                      (episode_id, project_id, version, "Demo 分集 1", synopsis, analysis["id"], now, now))
            source_scenes = data.get("scenes") or [{"title":"主要场景","setting":"待明确","description":synopsis}]
            scene_count = max(1, min(len(source_scenes), max(1, target // 15)))
            durations = [target // scene_count] * scene_count
            for i in range(target % scene_count): durations[i] += 1
            for sidx, source in enumerate(source_scenes[:scene_count], 1):
                scene_id = _id("scene"); duration = durations[sidx-1]
                c.execute("INSERT INTO scenes (id,episode_id,position,title,setting,purpose,action,dialogue,duration_sec,status) VALUES (?,?,?,?,?,?,?,?,?,'draft')",
                          (scene_id,episode_id,sidx,source.get("title",f"场景 {sidx}"),source.get("setting","待明确"),"推进故事节拍",source.get("description",synopsis),"",duration))
                shot_count = max(1, round(duration / 5)); shot_durations = [duration // shot_count] * shot_count
                for i in range(duration % shot_count): shot_durations[i] += 1
                for qidx, qduration in enumerate(shot_durations, 1):
                    action = source.get("description") or synopsis or "呈现故事动作"
                    c.execute("INSERT INTO shots (id,scene_id,position,duration_sec,shot_size,camera,action,narration_dialogue,render_strategy,status,revision,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,'draft',1,?,?)",
                              (_id("shot"),scene_id,qidx,qduration,"medium",("static" if qidx==1 else "slow_push_in"),action,"","static_pan_zoom",now,now))
            c.execute("UPDATE workflow_stages SET status='completed',updated_at=? WHERE project_id=? AND stage_key IN ('screenplay','script','storyboard')", (now,project_id))
        return self.get_script(project_id, version)

    def get_script(self, project_id: str, version: int | None = None) -> dict | None:
        with self.database.connect() as c:
            if version is None: vr = c.execute("SELECT MAX(version) FROM episodes WHERE project_id=?", (project_id,)).fetchone()[0]
            else: vr = version
            if vr is None: return None
            eps = c.execute("SELECT * FROM episodes WHERE project_id=? AND version=? ORDER BY position", (project_id,vr)).fetchall()
            project = c.execute("SELECT target_duration_sec FROM projects WHERE id=?", (project_id,)).fetchone()
            result_eps=[]; all_shots=[]
            for ep in eps:
                ev=dict(ep); ev["demo"]=bool(ev["demo"]); scenes=[]
                for scene in c.execute("SELECT * FROM scenes WHERE episode_id=? ORDER BY position", (ep["id"],)).fetchall():
                    sv=dict(scene); scenes.append(sv)
                    for shot in c.execute("SELECT * FROM shots WHERE scene_id=? ORDER BY position", (scene["id"],)).fetchall(): all_shots.append(self._shot(shot))
                ev["scenes"]=scenes; result_eps.append(ev)
        return {"project_id":project_id,"version":vr,"demo":True,"generator":"demo-deterministic-v1","target_duration_sec":project["target_duration_sec"],"episodes":result_eps,"shots":all_shots}

    @staticmethod
    def _shot(row):
        value=dict(row); value["order"]=value.pop("position"); value["duration"]=value.pop("duration_sec"); return value

    def list_shots(self, project_id: str) -> list[dict]:
        script=self.get_script(project_id); return script["shots"] if script else []

    def get_shot(self, shot_id: str) -> dict | None:
        with self.database.connect() as c:
            row=c.execute("SELECT * FROM shots WHERE id=?",(shot_id,)).fetchone()
        return self._shot(row) if row else None

    def update_shot(self, shot_id: str, payload: ShotUpdate) -> dict | None:
        changes=payload.model_dump(exclude_unset=True,exclude_none=True)
        mapping={"order":"position","duration":"duration_sec"}; changes={mapping.get(k,k):v for k,v in changes.items()}
        if not changes:
            with self.database.connect() as c: row=c.execute("SELECT * FROM shots WHERE id=?",(shot_id,)).fetchone()
            return self._shot(row) if row else None
        changes["updated_at"]=utc_now(); assignments=", ".join(f"{k}=?" for k in changes)+", revision=revision+1"
        try:
            with self.database.connect() as c:
                cur=c.execute(f"UPDATE shots SET {assignments} WHERE id=?",(*changes.values(),shot_id))
                if not cur.rowcount:return None
                row=c.execute("SELECT * FROM shots WHERE id=?",(shot_id,)).fetchone()
        except Exception as exc:
            if "UNIQUE constraint" in str(exc): raise ValueError("shot order already exists") from exc
            raise
        return self._shot(row)

    def approve_shot(self, shot_id: str) -> dict | None:
        return self.update_shot(shot_id, ShotUpdate(status="approved"))
