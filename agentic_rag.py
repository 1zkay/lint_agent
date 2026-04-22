"""
Hardware reference PDF Agentic RAG

实现方式对照 LangGraph 官方 agentic RAG 教程：
  1. PyPDFLoader 读取 PDF
  2. RecursiveCharacterTextSplitter 切分文档
  3. OpenAIEmbeddings + FAISS 建索引
  4. 基于 StateGraph / ToolNode / tools_condition 组装
     generate_query_or_respond -> retrieve -> grade -> rewrite -> generate

当前实现支持多个 PDF 知识库文档，每份文档独立构建索引，并在检索阶段合并结果。
这样既保留了页码级引用能力，也便于后续继续追加新的参考文档。

官方参考：
  - https://docs.langchain.com/oss/python/langgraph/agentic-rag
  - https://docs.langchain.com/oss/python/langchain/integrations/document_loaders/pypdfloader
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from langchain.tools import tool
from langchain_community.document_loaders import PyPDFLoader
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from llm_factory import (
    build_chat_model_from_config,
    build_openrouter_default_headers,
)

logger = logging.getLogger(__name__)


class PdfAgenticRagState(MessagesState):
    """Agentic RAG graph state."""

    rewrite_count: int


class GradeDocuments(BaseModel):
    """Binary relevance score for retrieved context."""

    binary_score: Literal["yes", "no"] = Field(
        description="Return 'yes' if the retrieved context is relevant, otherwise 'no'."
    )


@dataclass(frozen=True, slots=True)
class KnowledgeBaseSpec:
    """One PDF knowledge base entry."""

    kb_id: str
    title: str
    path: Path


@dataclass(frozen=True, slots=True)
class KnowledgeBaseRuntime:
    """Index and cache paths for a PDF knowledge base."""

    spec: KnowledgeBaseSpec
    index_dir: Path
    index_name: str
    vectorstore_path: Path
    docstore_path: Path
    metadata_path: Path


class HardwareReferenceAgenticRAGService:
    """Shared agentic RAG runtime for the built-in hardware reference PDFs."""

    def __init__(self, cfg: Any):
        self.cfg = cfg
        self.index_dir = Path(cfg.rag_index_dir).resolve()
        self.index_dir.mkdir(parents=True, exist_ok=True)

        self.knowledge_bases = self._load_knowledge_bases(cfg)
        if not self.knowledge_bases:
            raise FileNotFoundError("No available PDF knowledge base was found for RAG.")

        self.index_name = "hardware_reference_faiss"
        self.chunk_size = cfg.rag_chunk_size
        self.chunk_overlap = cfg.rag_chunk_overlap
        self.top_k = cfg.rag_top_k
        self.min_relevance = cfg.rag_min_relevance
        self.max_rewrites = cfg.rag_max_rewrites

        self._kb_runtimes = {
            spec.kb_id: self._build_runtime(spec)
            for spec in self.knowledge_bases
        }
        self._vectorstores: dict[str, FAISS] = {}
        self._vectorstore_locks = {
            spec.kb_id: asyncio.Lock()
            for spec in self.knowledge_bases
        }

        self._embeddings = self._build_embeddings(cfg)
        self._response_model = build_chat_model_from_config(cfg)
        self._graph = self._build_graph()

    @staticmethod
    def _slugify(value: str, default: str = "kb") -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
        return slug or default

    def _load_knowledge_bases(self, cfg: Any) -> tuple[KnowledgeBaseSpec, ...]:
        raw_items = list(getattr(cfg, "rag_pdf_paths", []) or [])
        specs: list[KnowledgeBaseSpec] = []
        seen_ids: set[str] = set()

        for raw_item in raw_items:
            path = Path(str(raw_item).strip()).resolve()
            if not path.exists():
                logger.warning("[agentic_rag] PDF knowledge base not found: %s", path)
                continue

            kb_id_base = self._slugify(path.stem, "kb")
            kb_id = kb_id_base
            suffix = 2
            while kb_id in seen_ids:
                kb_id = f"{kb_id_base}_{suffix}"
                suffix += 1
            seen_ids.add(kb_id)

            specs.append(
                KnowledgeBaseSpec(
                    kb_id=kb_id,
                    title=path.name,
                    path=path,
                )
            )

        return tuple(specs)

    def _build_runtime(self, spec: KnowledgeBaseSpec) -> KnowledgeBaseRuntime:
        kb_index_dir = self.index_dir / spec.kb_id
        kb_index_dir.mkdir(parents=True, exist_ok=True)
        return KnowledgeBaseRuntime(
            spec=spec,
            index_dir=kb_index_dir,
            index_name=self.index_name,
            vectorstore_path=kb_index_dir / f"{self.index_name}.faiss",
            docstore_path=kb_index_dir / f"{self.index_name}.pkl",
            metadata_path=kb_index_dir / "vectorstore.meta.json",
        )

    def _build_embeddings(self, cfg: Any) -> OpenAIEmbeddings:
        kwargs: dict[str, Any] = {
            "model": cfg.rag_embed_model,
            "base_url": cfg.rag_embed_base_url,
            "api_key": cfg.rag_embed_api_key or None,
            "chunk_size": cfg.rag_embed_batch_size,
            "timeout": cfg.llm_timeout,
        }
        default_headers = build_openrouter_default_headers(cfg.rag_embed_base_url, cfg)
        if default_headers:
            kwargs["default_headers"] = default_headers
        if cfg.rag_embed_base_url and "openai.com" not in cfg.rag_embed_base_url:
            # 非 OpenAI 官方端点（OpenRouter、硅基流动、本地等）均关闭 tiktoken，
            # 避免 cl100k_base 回落后解析失败；同时关闭长度安全分片，
            # 防止兼容端点收到 token-id 数组而非原始字符串批次。
            kwargs["tiktoken_enabled"] = False
            kwargs["check_embedding_ctx_length"] = False
        return OpenAIEmbeddings(**kwargs)

    def _index_metadata(self, runtime: KnowledgeBaseRuntime) -> dict[str, Any]:
        stat = runtime.spec.path.stat()
        return {
            "kb_id": runtime.spec.kb_id,
            "kb_title": runtime.spec.title,
            "pdf_name": runtime.spec.path.name,
            "pdf_size": stat.st_size,
            "embed_model": self.cfg.rag_embed_model.strip(),
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
        }

    def _index_is_fresh(self, runtime: KnowledgeBaseRuntime) -> bool:
        if (
            not runtime.vectorstore_path.exists()
            or not runtime.docstore_path.exists()
            or not runtime.metadata_path.exists()
        ):
            return False
        try:
            stored = json.loads(runtime.metadata_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        expected = self._index_metadata(runtime)
        expected["embed_model"] = str(expected["embed_model"]).strip().lower()
        stored["embed_model"] = str(stored.get("embed_model", "")).strip().lower()
        return all(stored.get(key) == value for key, value in expected.items())

    def _build_splitter(self) -> RecursiveCharacterTextSplitter:
        try:
            return RecursiveCharacterTextSplitter.from_tiktoken_encoder(
                chunk_size=self.chunk_size,
                chunk_overlap=self.chunk_overlap,
            )
        except Exception:
            return RecursiveCharacterTextSplitter(
                chunk_size=self.chunk_size,
                chunk_overlap=self.chunk_overlap,
            )

    def _load_pdf_pages_with_fitz(self, runtime: KnowledgeBaseRuntime) -> list[Document]:
        import fitz

        pdf = fitz.open(str(runtime.spec.path))
        docs: list[Document] = []
        for page_index in range(pdf.page_count):
            page = pdf.load_page(page_index)
            docs.append(
                Document(
                    page_content=page.get_text("text"),
                    metadata={"page": page_index},
                )
            )
        pdf.close()
        return docs

    async def _load_pdf_pages(self, runtime: KnowledgeBaseRuntime):
        # 官方文档区分 `page` / `single` 模式；这里显式使用逐页模式，
        # 因为后续检索结果需要稳定携带页码元数据。
        try:
            loader = PyPDFLoader(str(runtime.spec.path), mode="page")
            docs = await loader.aload()
        except Exception as exc:
            logger.warning(
                "[agentic_rag] PyPDFLoader failed for %s, fallback to PyMuPDF/fitz: %s",
                runtime.spec.path,
                exc,
            )
            docs = await asyncio.to_thread(self._load_pdf_pages_with_fitz, runtime)
        for doc in docs:
            page = int(doc.metadata.get("page", 0)) + 1
            doc.metadata["page_number"] = page
            doc.metadata["source"] = str(runtime.spec.path)
            doc.metadata["source_path"] = str(runtime.spec.path)
            doc.metadata["source_name"] = runtime.spec.path.name
            doc.metadata["kb_id"] = runtime.spec.kb_id
            doc.metadata["kb_title"] = runtime.spec.title
        return docs

    def _split_documents(self, runtime: KnowledgeBaseRuntime, docs):
        splitter = self._build_splitter()
        splits = splitter.split_documents(docs)
        for idx, doc in enumerate(splits):
            doc.metadata["chunk_id"] = f"{runtime.spec.kb_id}:{idx}"
            doc.metadata["source"] = str(runtime.spec.path)
            doc.metadata["source_path"] = str(runtime.spec.path)
            doc.metadata["source_name"] = runtime.spec.path.name
            doc.metadata["kb_id"] = runtime.spec.kb_id
            doc.metadata["kb_title"] = runtime.spec.title
            page = doc.metadata.get("page_number")
            if page is None and "page" in doc.metadata:
                doc.metadata["page_number"] = int(doc.metadata["page"]) + 1
        return splits

    async def ensure_vectorstore(self, kb_id: str) -> FAISS:
        vectorstore = self._vectorstores.get(kb_id)
        if vectorstore is not None:
            return vectorstore

        runtime = self._kb_runtimes[kb_id]
        async with self._vectorstore_locks[kb_id]:
            vectorstore = self._vectorstores.get(kb_id)
            if vectorstore is not None:
                return vectorstore

            if self._index_is_fresh(runtime):
                try:
                    logger.info(
                        "[agentic_rag] loading cached vector index for %s from %s",
                        runtime.spec.kb_id,
                        runtime.vectorstore_path,
                    )
                    vectorstore = await asyncio.to_thread(
                        FAISS.load_local,
                        str(runtime.index_dir),
                        self._embeddings,
                        runtime.index_name,
                        allow_dangerous_deserialization=True,
                    )
                    self._vectorstores[kb_id] = vectorstore
                    return vectorstore
                except Exception as exc:
                    logger.warning(
                        "[agentic_rag] failed to load cached vector index for %s, rebuilding: %s",
                        runtime.spec.kb_id,
                        exc,
                    )

            logger.info(
                "[agentic_rag] building vector index for %s from %s",
                runtime.spec.kb_id,
                runtime.spec.path,
            )
            docs = await self._load_pdf_pages(runtime)
            splits = await asyncio.to_thread(self._split_documents, runtime, docs)
            logger.info(
                "[agentic_rag] indexing %s pages into %s chunks for %s with model %s",
                len(docs),
                len(splits),
                runtime.spec.kb_id,
                self.cfg.rag_embed_model,
            )
            vectorstore = await FAISS.afrom_documents(
                documents=splits,
                embedding=self._embeddings,
            )
            await asyncio.to_thread(
                vectorstore.save_local,
                str(runtime.index_dir),
                runtime.index_name,
            )
            metadata = self._index_metadata(runtime) | {
                "page_count": len(docs),
                "chunk_count": len(splits),
            }
            await asyncio.to_thread(
                runtime.metadata_path.write_text,
                json.dumps(metadata, ensure_ascii=False, indent=2),
                "utf-8",
            )
            self._vectorstores[kb_id] = vectorstore
            logger.info("[agentic_rag] vector index ready for %s", runtime.spec.kb_id)
            return vectorstore

    async def ensure_vectorstores(self) -> dict[str, FAISS]:
        await asyncio.gather(
            *(self.ensure_vectorstore(spec.kb_id) for spec in self.knowledge_bases)
        )
        return dict(self._vectorstores)

    async def _search_one_runtime(
        self,
        runtime: KnowledgeBaseRuntime,
        query: str,
        *,
        fetch_k: int,
    ):
        vectorstore = await self.ensure_vectorstore(runtime.spec.kb_id)
        return await vectorstore.asimilarity_search_with_relevance_scores(
            query,
            k=fetch_k,
        )

    async def _retrieve_context_payload(
        self,
        query: str,
        *,
        top_k: int | None = None,
    ) -> dict[str, Any]:
        requested_k = max(1, int(top_k or self.top_k))
        fetch_k = max(4, requested_k * 2)
        runtimes = list(self._kb_runtimes.values())

        search_results = await asyncio.gather(
            *(
                self._search_one_runtime(runtime, query, fetch_k=fetch_k)
                for runtime in runtimes
            ),
            return_exceptions=True,
        )

        merged_results: list[dict[str, Any]] = []
        seen_chunks: set[tuple[str, str]] = set()

        for runtime, scored_docs in zip(runtimes, search_results):
            if isinstance(scored_docs, Exception):
                logger.warning(
                    "[agentic_rag] retrieval failed for %s: %s",
                    runtime.spec.kb_id,
                    scored_docs,
                )
                continue

            for doc, score in scored_docs:
                if score < self.min_relevance:
                    continue
                chunk_key = str(doc.metadata.get("chunk_id", ""))
                dedupe_key = (runtime.spec.kb_id, chunk_key)
                if dedupe_key in seen_chunks:
                    continue
                seen_chunks.add(dedupe_key)

                text = " ".join(str(doc.page_content or "").split())
                merged_results.append(
                    {
                        "kb_id": runtime.spec.kb_id,
                        "kb_title": runtime.spec.title,
                        "source_name": str(doc.metadata.get("source_name", runtime.spec.path.name)),
                        "source_path": str(doc.metadata.get("source_path", runtime.spec.path)),
                        "page": int(doc.metadata.get("page_number", 0) or 0),
                        "score": round(float(score), 4),
                        "content": text,
                    }
                )

        merged_results.sort(key=lambda item: float(item["score"]), reverse=True)
        results = merged_results[:requested_k]

        return {
            "query": query,
            "source": "built-in hardware reference collection",
            "knowledge_bases": [
                {
                    "kb_id": runtime.spec.kb_id,
                    "title": runtime.spec.title,
                    "path": str(runtime.spec.path),
                }
                for runtime in runtimes
            ],
            "results": results,
            "message": (
                "ok"
                if results
                else "No sufficiently relevant context found in the configured hardware references."
            ),
        }

    def _build_graph(self):
        @tool("retrieve_hardware_reference_context")
        async def retrieve_hardware_reference_context(query: str) -> str:
            """Search the built-in hardware reference PDFs for IEEE language or Vivado synthesis context."""

            payload = await self._retrieve_context_payload(query)
            return json.dumps(payload, ensure_ascii=False)

        retriever_tool = retrieve_hardware_reference_context

        async def generate_query_or_respond(state: PdfAgenticRagState):
            system_message = SystemMessage(
                content=(
                    "You are a hardware design reference assistant. "
                    "The built-in hardware reference collection currently includes the IEEE standard "
                    "and Vivado synthesis documentation. "
                    "Prefer using the retriever tool for legacy Verilog constructs, SystemVerilog language "
                    "rules, and Xilinx/Vivado synthesis behavior, coding style, attributes, or lint-diagnosis "
                    "questions. Only answer directly if retrieval is clearly unnecessary."
                )
            )
            response = await (
                self._response_model.bind_tools([retriever_tool]).ainvoke(
                    [system_message, *state["messages"]]
                )
            )
            return {"messages": [response]}

        async def grade_documents(
            state: PdfAgenticRagState,
        ) -> Literal["generate_answer", "rewrite_question"]:
            tool_message = next(
                (msg for msg in reversed(state["messages"]) if isinstance(msg, ToolMessage)),
                None,
            )
            if tool_message is None:
                return "rewrite_question"

            try:
                payload = json.loads(str(tool_message.content))
            except Exception:
                payload = {"results": [], "raw": str(tool_message.content)}

            if not payload.get("results"):
                if int(state.get("rewrite_count", 0) or 0) >= self.max_rewrites:
                    return "generate_answer"
                return "rewrite_question"

            question = str(state["messages"][0].content)
            prompt = (
                "You are a grader assessing whether retrieved hardware-reference context is relevant "
                "to the user's question.\n"
                "If the context contains clauses, synthesis guidance, semantics, or terminology that can "
                "help answer the question, return 'yes'. Otherwise return 'no'.\n\n"
                f"Question:\n{question}\n\n"
                f"Retrieved context JSON:\n{json.dumps(payload, ensure_ascii=False)}"
            )
            response = await (
                self._response_model
                .with_structured_output(GradeDocuments)
                .ainvoke([{"role": "user", "content": prompt}])
            )
            if response.binary_score == "yes":
                return "generate_answer"
            if int(state.get("rewrite_count", 0) or 0) >= self.max_rewrites:
                return "generate_answer"
            return "rewrite_question"

        async def rewrite_question(state: PdfAgenticRagState):
            question = str(state["messages"][0].content)
            prompt = (
                "请改写下面这个关于 Verilog/SystemVerilog 语言规则或 Vivado 综合行为的问题，"
                "让它更适合做语义检索。保留关键信号、语法构造、综合现象、工具关键词、"
                "告警现象和代码上下文。只输出改写后的单句查询。\n\n"
                f"原始问题：\n{question}"
            )
            response = await self._response_model.ainvoke(
                [{"role": "user", "content": prompt}]
            )
            return {
                "messages": [HumanMessage(content=str(response.content).strip())],
                "rewrite_count": int(state.get("rewrite_count", 0) or 0) + 1,
            }

        async def generate_answer(state: PdfAgenticRagState):
            question = str(state["messages"][0].content)
            tool_message = next(
                (msg for msg in reversed(state["messages"]) if isinstance(msg, ToolMessage)),
                None,
            )
            context_json = str(tool_message.content) if tool_message else '{"results":[]}'
            prompt = (
                "你是 Verilog / SystemVerilog / 硬件综合参考文档问答助手。"
                "知识库同时覆盖 IEEE 标准和 Vivado 综合文档。"
                "优先依据给定的检索上下文作答，不要把未检索到的内容说成文档明确规定。"
                "如果证据不足，要明确说明。"
                "默认用中文回答；若用户明确要求英文再切换。"
                "引用页码时使用 [文档名 p.X] 形式，其中 X 来自 context JSON 里的 page 字段。"
                "当问题属于语言语义时优先引用 IEEE；当问题属于 Xilinx/Vivado 综合行为、"
                "属性、约束或工具策略时优先引用 Vivado。"
                "回答要简洁，但要足够支撑 lint 诊断或规则判断。\n\n"
                f"问题：\n{question}\n\n"
                f"context JSON:\n{context_json}"
            )
            response = await self._response_model.ainvoke(
                [{"role": "user", "content": prompt}]
            )
            return {"messages": [response]}

        workflow = StateGraph(PdfAgenticRagState)
        workflow.add_node("generate_query_or_respond", generate_query_or_respond)
        workflow.add_node("retrieve", ToolNode([retriever_tool]))
        workflow.add_node("rewrite_question", rewrite_question)
        workflow.add_node("generate_answer", generate_answer)

        workflow.add_edge(START, "generate_query_or_respond")
        workflow.add_conditional_edges(
            "generate_query_or_respond",
            tools_condition,
            {
                "tools": "retrieve",
                END: END,
            },
        )
        workflow.add_conditional_edges(
            "retrieve",
            grade_documents,
        )
        workflow.add_edge("rewrite_question", "generate_query_or_respond")
        workflow.add_edge("generate_answer", END)

        return workflow.compile()

    async def ask(self, question: str) -> str:
        if not question.strip():
            raise ValueError("question must not be empty")

        result = await self._graph.ainvoke(
            {
                "messages": [HumanMessage(content=question.strip())],
                "rewrite_count": 0,
            },
        )
        messages = list(result["messages"])

        final_answer = ""
        for message in reversed(messages):
            if isinstance(message, AIMessage) and not message.tool_calls:
                final_answer = str(message.content)
                break
        if not final_answer:
            for message in reversed(messages):
                if isinstance(message, AIMessage):
                    final_answer = str(message.content)
                    break

        rewritten_question = None
        human_messages = [
            str(message.content)
            for message in messages
            if isinstance(message, HumanMessage)
        ]
        if len(human_messages) > 1:
            rewritten_question = human_messages[-1]

        last_tool_payload: dict[str, Any] | None = None
        for message in reversed(messages):
            if isinstance(message, ToolMessage):
                try:
                    last_tool_payload = json.loads(str(message.content))
                except Exception:
                    last_tool_payload = {"results": [], "raw": str(message.content)}
                break

        citations = []
        if last_tool_payload:
            for item in last_tool_payload.get("results", []):
                citations.append(
                    {
                        "kb_id": item.get("kb_id"),
                        "kb_title": item.get("kb_title"),
                        "source_name": item.get("source_name"),
                        "page": item.get("page"),
                        "score": item.get("score"),
                        "snippet": str(item.get("content", ""))[:280],
                    }
                )

        payload = {
            "question": question.strip(),
            "rewritten_question": rewritten_question,
            "used_retrieval": last_tool_payload is not None,
            "answer": final_answer,
            "citations": citations[: self.top_k],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _runtime_index_summary(self, runtime: KnowledgeBaseRuntime) -> dict[str, Any]:
        summary = {
            "kb_id": runtime.spec.kb_id,
            "title": runtime.spec.title,
            "index_path": str(runtime.vectorstore_path),
            "ready": runtime.vectorstore_path.exists(),
        }
        if not runtime.metadata_path.exists():
            return summary
        try:
            metadata = json.loads(runtime.metadata_path.read_text(encoding="utf-8"))
        except Exception:
            metadata = {}
        return summary | metadata

    def index_summary(self) -> dict[str, Any]:
        knowledge_bases = [
            self._runtime_index_summary(runtime)
            for runtime in self._kb_runtimes.values()
        ]
        return {
            "index_root": str(self.index_dir),
            "ready": bool(knowledge_bases) and all(item["ready"] for item in knowledge_bases),
            "knowledge_bases": knowledge_bases,
        }


_SERVICE_CACHE: dict[tuple[Any, ...], HardwareReferenceAgenticRAGService] = {}
_SERVICE_CACHE_LOCK = threading.Lock()


def _embedding_credentials_available(cfg: Any) -> bool:
    if str(getattr(cfg, "rag_embed_api_key", "")).strip():
        return True
    base_url = str(getattr(cfg, "rag_embed_base_url", "")).strip()
    if not base_url:
        return False
    host = (urlparse(base_url).hostname or "").strip().lower()
    return host in {"localhost", "127.0.0.1", "0.0.0.0"}


def _service_cache_key(cfg: Any) -> tuple[Any, ...]:
    kb_key = tuple(
        str(Path(str(item).strip()).resolve())
        for item in list(getattr(cfg, "rag_pdf_paths", []) or [])
        if str(item).strip()
    )

    return (
        bool(cfg.rag_enabled),
        kb_key,
        str(Path(cfg.rag_index_dir).resolve()),
        cfg.rag_embed_model,
        cfg.rag_embed_base_url,
        cfg.llm_model,
        cfg.llm_base_url,
        cfg.rag_chunk_size,
        cfg.rag_chunk_overlap,
        cfg.rag_top_k,
        cfg.rag_min_relevance,
        cfg.rag_max_rewrites,
    )


def get_hardware_reference_agentic_rag_service(cfg: Any) -> HardwareReferenceAgenticRAGService:
    key = _service_cache_key(cfg)
    with _SERVICE_CACHE_LOCK:
        service = _SERVICE_CACHE.get(key)
        if service is None:
            service = HardwareReferenceAgenticRAGService(cfg)
            _SERVICE_CACHE[key] = service
        return service


def build_hardware_reference_agentic_rag_tool(cfg: Any):
    """Build the hardware-reference tool exposed to the main Chainlit agent."""

    if not cfg.rag_enabled:
        logger.info("[agentic_rag] disabled by config")
        return None
    if not cfg.llm_model:
        logger.warning("[agentic_rag] chat model not configured; RAG tool disabled")
        return None
    if not _embedding_credentials_available(cfg):
        logger.warning("[agentic_rag] embedding credentials missing; RAG tool disabled")
        return None

    service = get_hardware_reference_agentic_rag_service(cfg)

    @tool("query_reference_docs")
    async def query_reference_docs(question: str) -> str:
        """Use the built-in hardware reference knowledge bases to answer Verilog/SystemVerilog semantics and Vivado synthesis questions with page citations."""

        return await service.ask(question)

    return query_reference_docs
