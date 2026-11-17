import os, sys, warnings, pytest
warnings.filterwarnings("ignore")
ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)
IDX = os.path.join(ROOT, ".data", "test_index")
os.environ.update(CORPUS_DIR=os.path.join(ROOT, "corpus"), INDEX_DIR=IDX, LLM_BASE_URL="inprocess://heuristic",
                  EMBEDDING_BACKEND="hashing", LOG_LEVEL="WARNING")


@pytest.fixture(scope="session")
def retriever():
    from app.corpus.synth import build
    os.makedirs(os.path.join(ROOT, "corpus", "raw"), exist_ok=True)
    build(f"{ROOT}/corpus/source/regulations.txt", f"{ROOT}/corpus/raw/academic_regulations_2024.pdf")
    build(f"{ROOT}/corpus/source/bye_laws.txt", f"{ROOT}/corpus/raw/examination_bye_laws_2024.pdf")
    from app.corpus.pipeline import run_index
    run_index()
    from app.corpus.retriever import Retriever
    return Retriever(IDX)
