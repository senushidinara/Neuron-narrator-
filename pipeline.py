# pipeline.py
from flask import Flask, request, jsonify
import os
import time
import json
import logging
import requests
from elasticsearch import Elasticsearch, helpers
from google.cloud import bigquery
from google.cloud import aiplatform
from google.cloud.aiplatform.gapic import PredictionServiceClient

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("neuronattaror-service")

# Env config
FIVETRAN_API_KEY = os.environ.get("FIVETRAN_API_KEY")
FIVETRAN_API_SECRET = os.environ.get("FIVETRAN_API_SECRET")
FIVETRAN_CONNECTOR_ID = os.environ.get("FIVETRAN_CONNECTOR_ID")

GCP_PROJECT = os.environ.get("GCP_PROJECT")
BIGQUERY_DATASET = os.environ.get("BIGQUERY_DATASET", "nueronattaror")
BIGQUERY_TABLE = os.environ.get("BIGQUERY_TABLE", "eeg_summaries")

ELASTIC_URL = os.environ.get("ELASTIC_URL")
ELASTIC_USER = os.environ.get("ELASTIC_USER")
ELASTIC_PASSWORD = os.environ.get("ELASTIC_PASSWORD")
ELASTIC_INDEX_EVIDENCE = os.environ.get("ELASTIC_INDEX_EVIDENCE", "nueronattaror_evidence")
ELASTIC_INDEX_EXPLANATIONS = os.environ.get("ELASTIC_INDEX_EXPLANATIONS", "nueronattaror_explanations")

GCP_REGION = os.environ.get("GCP_REGION", "us-central1")
VERTEX_MODEL_NAME = os.environ.get("VERTEX_MODEL_NAME")

# Initialize clients
bq_client = bigquery.Client(project=GCP_PROJECT)
es = Elasticsearch([ELASTIC_URL], basic_auth=(ELASTIC_USER, ELASTIC_PASSWORD), timeout=30)
aiplatform.init(project=GCP_PROJECT, location=GCP_REGION)
prediction_client = PredictionServiceClient()


def trigger_fivetran_sync(connector_id: str):
    if not connector_id or not FIVETRAN_API_KEY:
        logger.warning("Fivetran not configured")
        return {"status": "not_configured"}
    url = f"https://api.fivetran.com/v1/connectors/{connector_id}/sync"
    r = requests.post(url, auth=(FIVETRAN_API_KEY, FIVETRAN_API_SECRET), timeout=30)
    logger.info("Fivetran sync triggered, status=%s", r.status_code)
    return r.json() if r.headers.get("content-type","").startswith("application/json") else {"status_code": r.status_code}


def fetch_records(patient_token=None, limit=20):
    q = f"""
    SELECT record_id, patient_token, age_range, symptoms, note_text, eeg_features, uploaded_at
    FROM `{GCP_PROJECT}.{BIGQUERY_DATASET}.{BIGQUERY_TABLE}`
    ORDER BY uploaded_at DESC
    LIMIT {limit}
    """
    if patient_token:
        q = q.replace("ORDER BY", f"WHERE patient_token='{patient_token}' ORDER BY")
    df = bq_client.query(q).to_dataframe()
    return df.to_dict(orient="records")


def create_evidence_doc(row):
    doc = {
        "id": row.get("record_id"),
        "patient_token": row.get("patient_token"),
        "age_range": row.get("age_range"),
        "symptoms": row.get("symptoms"),
        "note_text": row.get("note_text")[:4000] if row.get("note_text") else "",
        "eeg_features": row.get("eeg_features"),
        "uploaded_at": row.get("uploaded_at").isoformat() if row.get("uploaded_at") else None,
        "provenance": {"source": f"bigquery:{GCP_PROJECT}.{BIGQUERY_DATASET}.{BIGQUERY_TABLE}"}
    }
    return doc


def bulk_index(docs):
    if not es.indices.exists(index=ELASTIC_INDEX_EVIDENCE):
        mapping = {"mappings": {"properties": {"id": {"type": "keyword"}, "patient_token": {"type": "keyword"}, "note_text": {"type":"text"}, "uploaded_at": {"type":"date"}}}}
        es.indices.create(index=ELASTIC_INDEX_EVIDENCE, body=mapping)
    actions = [{"_index": ELASTIC_INDEX_EVIDENCE, "_id": d["id"], "_source": d} for d in docs]
    helpers.bulk(es, actions)


def search_evidence(patient_token, k=5):
    q = {"size": k, "query": {"term": {"patient_token": patient_token}}, "sort": [{"uploaded_at": {"order": "desc"}}]}
    res = es.search(index=ELASTIC_INDEX_EVIDENCE, body=q)
    hits = [h["_source"] for h in res.get("hits", {}).get("hits", [])]
    return hits


def generate_with_vertex(patient_token, evidence_list):
    # Build prompt
    evidence_texts = []
    for i, ev in enumerate(evidence_list, start=1):
        evidence_texts.append(f"[E{i}] id:{ev.get('id')} uploaded:{ev.get('uploaded_at')}\nNOTE: {ev.get('note_text')[:400]}\nEEG: {json.dumps(ev.get('eeg_features'))}\n")
    context = "\n\n".join(evidence_texts)
    system = ("You are NeuroNattaraor. Translate the evidence into a simple, non-alarming explanation for a parent. "
              "Include evidence tags [E1], [E2] for each factual sentence. Provide Next steps and Confidence (0-1).")
    prompt = system + "\n\nEVIDENCE:\n" + context + "\n\nTASK: Generate JSON with fields: summary, next_steps (list), safety_flags (list of {level,reason}), confidence (float)."

    instance = {"content": prompt}
    # Note: Vertex call wrapper -- if this fails for your model, swap with gemini REST example in /examples
    endpoint = prediction_client.endpoint_path(project=GCP_PROJECT, location=GCP_REGION, endpoint=VERTEX_MODEL_NAME.split("/")[-1])
    from google.cloud.aiplatform_v1.types import PredictRequest
    request = PredictRequest(endpoint=endpoint, instances=[instance])
    response = prediction_client.predict(request=request)
    # Try to gather text
    prediction_text = ""
    for p in response.predictions:
        if isinstance(p, dict) and "content" in p:
            prediction_text += p["content"]
        else:
            prediction_text += str(p)
    # Parse JSON if possible
    parsed = {}
    try:
        start = prediction_text.find("{")
        end = prediction_text.rfind("}")
        parsed = json.loads(prediction_text[start:end+1])
    except Exception:
        parsed = {"raw_text": prediction_text}
    parsed["evidence_ids"] = [ev["id"] for ev in evidence_list]
    return parsed


def save_explanation(patient_token, explanation):
    doc = {"patient_token": patient_token, "explanation": explanation, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if not es.indices.exists(index=ELASTIC_INDEX_EXPLANATIONS):
        es.indices.create(index=ELASTIC_INDEX_EXPLANATIONS, body={"mappings":{"properties":{"patient_token":{"type":"keyword"},"generated_at":{"type":"date"}}}})
    es.index(index=ELASTIC_INDEX_EXPLANATIONS, document=doc)
    return doc


@app.route("/run_pipeline", methods=["POST"])
def run_pipeline():
    body = request.json or {}
    patient_token = body.get("patient_token")
    trigger_fivetran = body.get("trigger_fivetran", False)
    if trigger_fivetran and FIVETRAN_CONNECTOR_ID:
        trigger_fivetran_sync(FIVETRAN_CONNECTOR_ID)
        # optionally wait or poll for status in production

    rows = fetch_records(patient_token=patient_token, limit=20)
    if not rows:
        return jsonify({"error": "No records found"}), 404

    docs = [create_evidence_doc(r) for r in rows]
    bulk_index(docs)

    # use first patient's token / first row for demo
    demo_token = patient_token or rows[0].get("patient_token")
    evidence_hits = search_evidence(demo_token, k=5)
    explanation = generate_with_vertex(demo_token, evidence_hits)
    saved = save_explanation(demo_token, explanation)
    return jsonify({"patient_token": demo_token, "explanation": explanation, "evidence_docs": evidence_hits})


@app.route("/health")
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
