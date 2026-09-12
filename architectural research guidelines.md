# WAX INTELLIGENCE — RESEARCH & DEVELOPMENT ROADMAP

---

## PART I — EXECUTIVE EXPLANATION

### What WAX Is Trying to Become

WAX is not trying to build a better tutor. That is the single most important thing to understand before reading anything else in this document. The tutor was the first use case that exposed the real problem — which is that when you build a system around a use case, the use case becomes the walls, the ceiling, and the floor. The intelligence inside it can only move in the directions the walls permit.

What WAX is actually investigating is whether it is possible to build a digital environment — a runtime, an operating context, a persistent space — inside which an AI can encounter a human being's objective, understand it, and work toward it, without the builders having pre-scripted every category of objective that the human is allowed to bring.

The word "environment" is doing a lot of work in that sentence and it deserves to be unpacked. When you sit down at a computer, you do not tell the operating system in advance that today you plan to write a letter, then check your email, then look something up, then build a spreadsheet. The computer does not have a "letter workflow" or an "email mode" or a "research funnel." It has an environment — a file system, network access, a display, input devices, a set of running processes — and you bring whatever objective you have. The environment does not pre-approve your objectives. It provides mechanisms, enforces resource constraints, handles identity, manages memory, and mediates access to hardware. You — the intelligence sitting in front of it — decide how to use the environment to accomplish what you came to do.

WAX wants to create the equivalent of that environment, but where the intelligence sitting inside it is not a human being — it is an AI. The AI is the one reasoning about objectives and determining how to use what is available. The human being is the one arriving with objectives. And the environment — WAX's runtime — is what makes it possible for the AI to act reliably, persistently, securely, and continuously across time, across interfaces, and across the full range of things a human might legitimately want to do in the digital world.

The first application domain is education, and that is not a compromise — it is a genuine strategic priority. Nigerian students preparing for WAEC, NECO, JAMB, and BECE are the first humans this environment is meant to serve. But the environment should not be shaped around the fact that the first humans are students, any more than the internet was designed around the fact that the first users were military researchers.

### Why This Is Different from an Ordinary AI Tutor or Chatbot

An ordinary AI tutor works like this: a student types a question. The AI answers it. Maybe the AI asks a follow-up question. Maybe it stores a preference. The conversation ends. The next conversation starts again from scratch or with a thin context window. The student is understood as a student. The conversation is understood as a tutoring session. The system never needs to do anything that is not tutoring, because it was built to tutor.

An ordinary chatbot works similarly: the human is understood as a user. The conversation is understood as a support interaction. The system does what chatbots do — it answers, deflects, escalates, or closes tickets.

WAX is investigating something that has no stable, widely-deployed production equivalent yet. The closest analogies that exist in the real world are:

Research-grade systems like DeepMind's systems that pursue objectives across time. Agentic systems like early versions of AutoGPT, BabyAGI, LangChain agents, or CrewAI, which attempted multi-step objective pursuit but have significant reliability, security, and continuity problems. Commercial agent platforms like Anthropic's Claude with tools, OpenAI's GPT with function calling, and various orchestration layers built on top. Operating-system analogies like E2B, Modal, Fly.io, and similar platforms that allow code to run in isolated sandboxes. Agent runtimes like LangGraph, Temporal-based orchestration, and research systems like the SWE-agent framework.

None of these is exactly what WAX wants to be. Each solves part of the problem while leaving major parts unsolved or poorly addressed. That is not a discouragement — it is the honest assessment of where the field is, and it means there is a genuine intellectual and engineering opportunity here.

The fundamental difference between WAX and all of these is the combination of properties WAX is trying to hold simultaneously: persistent identity and memory across time, open-world objective pursuit without pre-specified categories, interface-agnosticism so the same intelligence is reachable from WhatsApp or a full web environment or voice or anything else, dynamic capability discovery so the AI can reason about what it can and cannot do and what new capabilities might be obtained, long-running work that survives interruption and can be resumed intelligently, and security strong enough to allow significant AI autonomy without granting the AI sovereignty over the environment itself.

Most existing systems satisfy two or three of these properties. Satisfying all of them simultaneously, at scale, in a production system serving real humans, is the actual research problem.

### The Fundamental Technical Problem

Here is the core tension that runs through every part of this research:

The more open-ended the environment, the harder it is to make the AI reliable. And the more reliable you make the AI, the more you are tempted to constrain the environment — to define fixed workflows, fixed tools, fixed categories — and you are back where you started.

This tension is not merely a software design problem. It runs through epistemology (how does the AI know what it knows?), through distributed systems (how do you guarantee consistency when the AI might take thousands of steps across many services?), through security (how do you give something significant autonomy without it becoming a threat?), through human-computer interaction (how does a person maintain comprehension and control over a system that is doing complex work on their behalf?), and through AI alignment (how do you make an AI that pursues the actual human objective rather than a proxy for it?).

WAX is not trying to solve AI alignment as a fundamental research problem. But it must take seriously enough insights from alignment research to build a system that behaves appropriately within the boundaries its builders intend.

The most honest way to state the technical problem is this: WAX is trying to build a system in which an AI can be trusted to work on behalf of a human, persistently and across a wide range of objectives, within an environment that is powerful enough to be genuinely useful and constrained enough to be genuinely safe. Research must determine whether that is achievable, how close existing systems have come, what the unsolved problems are, and what architecture makes the combination possible.

---

## PART II — DEPENDENCY MAP

The following is a conceptual dependency graph. Read it as: "you cannot responsibly make decisions about item B until you understand item A." This is not a strict sequential list — several branches can be researched in parallel — but the dependencies within each branch are real.

Branch 1 — Intelligence Foundation:
What does "AI reasoning about an objective" actually mean technically? → What tool-use and planning mechanisms exist? → How reliable are they? → What are their failure modes?

Branch 2 — Runtime and Environment:
What is the minimum environment an intelligence needs to act in the digital world? → How do you represent the state of ongoing work? → How do you persist state across time? → How do you resume interrupted work? → How do you handle failures?

Branch 3 — Memory and Continuity:
What kinds of memory does an AI need? → How do you store, retrieve, compress, and forget information? → How do you handle conflicting memories? → How do you build a persistent identity across sessions?

Branch 4 — Capability Architecture:
How does an AI discover what it can do? → How does it acquire new capabilities safely? → How does it integrate external services? → How does it reason about capability gaps?

Branch 5 — Security and Authorization:
What does it mean for an AI to have permissions? → How do you enforce those permissions at the environment level? → How do you prevent the AI from being manipulated into doing things it should not? → How do you audit everything?

Branch 6 — Execution and Isolation:
What happens when the AI needs to run code, use a browser, or produce outputs? → How do you isolate that execution? → How do you resource-constrain it? → How do you give the AI results back?

Branch 7 — Education Mission:
Given everything above, how does WAX serve students extraordinarily well as its first and primary purpose? → How does the intelligence design for depth of understanding rather than speed of answer? → What research exists on AI-assisted learning?

Dependencies between branches:

Branch 2 (Runtime) depends on having enough understanding from Branch 1 (Intelligence) to know what the runtime needs to support. You cannot design the aircraft until you understand what the pilot needs to do.

Branch 3 (Memory) depends on Branch 2 (Runtime) because memory must live somewhere and be accessed by something.

Branch 4 (Capability) depends on Branch 5 (Security) because capability acquisition without security is just privilege escalation.

Branch 6 (Execution) depends on Branch 2 (Runtime) and Branch 5 (Security). You cannot safely give an AI code execution without both.

Branch 7 (Education) can be researched in parallel with all the above because the pedagogical research — what makes good AI-assisted learning — does not depend on the architecture. It only needs to be integrated at the end.

Branch 5 (Security) is not really a dependent stage — it is a cross-cutting concern that touches every other branch and must be considered from the beginning, not added later.

---

## PART III — FULL STAGED ROADMAP

---

### STAGE 0 — CONCEPTUAL GROUNDING
**Objective:** Before researching technology, establish absolute conceptual clarity about the problem. This stage is about understanding the philosophy well enough that when you encounter technical solutions, you can evaluate them against the actual problem rather than the first problem description you read.

**Why it comes first:** Every subsequent stage involves encountering systems, papers, frameworks, and tools that were built to solve adjacent problems. Without clarity about the WAX problem, you will be perpetually tempted to adopt the nearest available solution and retrofit the philosophy around it. That is how WAX PREP became scripted: technology was adopted before the philosophy was clear.

**What this means in plain English:** Before you research how to build the thing, make absolutely sure you understand what the thing is supposed to be. Not at the level of features — at the level of the problem it is solving and the fundamental properties it must have.

**Why WAX cares:** The WAX foundation document was written precisely because implementation had begun before conceptual clarity was achieved. Stage 0 is the stage that document was trying to create. Conducting it properly before any new research begins is the most important intellectual discipline in the project.

**What an engineer would call it:** Requirements clarification, problem definition, north star alignment, first-principles analysis.

**Research questions for this stage:**
- What are the fundamental properties WAX must have, and are they actually compatible with each other?
- What is the minimum viable definition of "general-purpose intelligent environment" that distinguishes WAX from both a chatbot and a general-purpose computer?
- What does "open world" mean precisely, technically? Is it achievable, or is every real system actually a closed world that is merely large?
- What is the difference between "the AI does everything" as described in the WAX document and "the AI has unrestricted autonomy"? Can that difference be made precise and enforceable?
- Can WAX's philosophy be formalized into a set of testable architectural principles — things that, if violated, would mean WAX has become what it is trying not to be?
- Is the school analogy sound? Does it hold up under pressure? What does it break down on?
- Is the airplane analogy sound? What does it capture that the school analogy misses, and vice versa?

**Expected output of this stage:** A document called the WAX Conceptual Invariants — a concise set of stated properties that the system must have, stated in testable terms, with a section on which properties are in tension with each other and why. This document becomes the measuring stick for everything that follows.

**Exit criteria:** You should be able to describe WAX in one paragraph in a way that a senior engineer who has never heard of the project would understand, and that description should immediately distinguish WAX from chatbots, agent frameworks, tutoring systems, and general-purpose computers. If you cannot do this, you do not yet understand the problem clearly enough to research solutions.

**What should remain undecided:** Everything about implementation. No technology names. No database choices. No framework decisions. No model providers.

**Common mistakes to avoid:** Confusing ambition with clarity. Writing a longer description of the vision does not mean the conceptual problem is better understood. The output of this stage should be shorter and more precise than the WAX Foundation document, not longer and more expansive.

---

### STAGE 1 — AI AGENT AND TOOL-USE LANDSCAPE SURVEY
**Objective:** Develop a thorough, accurate, and honest understanding of the current state of AI agents, tool use, and autonomous AI systems. Understand what exists, what works, what fails, and why.

**Why it comes at this point:** WAX's architecture must be built on top of real AI capabilities, not the capabilities imagined by demo videos and marketing materials. Stage 1 establishes the real floor — what AI systems can actually do today, what they reliably cannot do, and what the field considers possible in the near term.

**What this means in plain English:** An "AI agent" is an AI system that can take sequences of actions to accomplish a goal, rather than just answering a single question. "Tool use" means the AI can call external functions, APIs, or services as part of working toward a goal. Before you design a system that depends on these things, you need to understand how good they actually are in practice.

**Why WAX cares:** WAX's entire premise depends on an AI that can reason about objectives, decide what actions to take, use available tools, recognize gaps, and course-correct. If current AI systems cannot do this reliably, WAX needs to know that now — not after building an architecture around the assumption that they can.

**What an engineer would call it:** Technology landscape analysis, state-of-the-art survey, capability assessment, failure mode analysis.

**Prerequisite knowledge to build first:** Basic understanding of how large language models work at a high level — not the mathematics, but the conceptual mechanics: they produce text based on patterns in training data and context. Understanding that LLMs are stateless — they have no memory between API calls — is foundational. Every form of memory and continuity in an agent system is something built on top of the stateless model, not something the model inherently has.

**Research questions:**
- What is the precise definition of an "AI agent" in the current technical literature? Does WAX match this definition, or does it require a more expansive concept?
- What are the major existing agent frameworks (LangChain, LangGraph, AutoGPT, BabyAGI, CrewAI, Microsoft AutoGen, Semantic Kernel, Anthropic's tool-use, OpenAI function calling, Google's Vertex AI agents) and what problem does each solve?
- How do current AI systems handle multi-step reasoning? What mechanisms exist for planning? What are ReAct, Chain-of-Thought, Tree-of-Thought, and similar techniques?
- What are the documented failure modes of AI agents in production? When do they get stuck in loops? When do they hallucinate tool outputs? When do they fail to recover from errors? When do they pursue the wrong interpretation of an objective?
- How do AI agents currently handle tool use? What is the technical mechanism for calling a tool, receiving a result, and incorporating the result into the next decision?
- What is the difference between a "tool-calling model" and an "agent"? Is every model with tool calling an agent, or is the distinction meaningful?
- What does current research say about the reliability of AI agents for long-horizon tasks — tasks requiring many steps? What is the typical failure rate at various task lengths?
- What is an "AI agent runtime" or "agent framework"? How does it differ from simply calling an API?
- What existing production systems are most like WAX's ambition? What have they achieved and where do they fall short?
- What is "agentic AI" and how does academic research on it relate to commercial implementations?

**Technologies and systems to investigate:**
- LangChain and LangGraph (open-source agent orchestration, widely used, many documented failure modes)
- Microsoft AutoGen (multi-agent conversation framework)
- CrewAI (role-based multi-agent framework)
- Anthropic's Claude with tools (how tool use is actually implemented at the API level)
- OpenAI function calling and Assistants API (their approach to agents and threads)
- Google Gemini with function calling
- SWE-agent (academic system for software engineering tasks, good research on agent reliability)
- AgentBench (benchmark for AI agents)
- GAIA benchmark (general AI assistants benchmark)
- Toolformer (research on models learning to use tools)
- ReAct (Reasoning and Acting — foundational agent architecture paper)
- Chain-of-Thought prompting (Wei et al., foundational)
- HuggingGPT / TaskMatrix (systems that route to specialized models)
- Voyager (AI agent in Minecraft — good case study of open-world exploration)

**Key papers and resources to study:**
- "ReAct: Synergizing Reasoning and Acting in Language Models" (Yao et al., 2022)
- "Toolformer: Language Models Can Teach Themselves to Use Tools" (Schick et al., 2023)
- "AgentBench: Evaluating LLMs as Agents" (Liu et al., 2023)
- "A Survey on Large Language Model based Autonomous Agents" (Wang et al., 2023)
- "The Rise and Potential of Large Language Model Based Agents: A Survey" (Xi et al., 2023)
- Anthropic's documentation on tool use
- OpenAI's documentation on function calling and the Assistants API

**Expected discoveries:** You will likely discover that existing agent frameworks are better at short, well-defined tasks than at long, open-ended ones. You will likely discover that current systems have significant issues with error recovery, loop detection, and knowing when to stop. You will likely discover that the field is moving very fast and that capabilities available six months ago are significantly different from capabilities available today.

**What discoveries would change the architecture:** If you discover that AI models fundamentally cannot reliably pursue open-ended multi-step objectives without human intervention at every decision point, WAX needs to account for that — either by designing more human-in-the-loop checkpoints than the philosophy envisions, or by waiting for model capabilities to mature further. This is a potential fundamental constraint on the entire project.

**Expected deliverable:** A Research Report titled "Current State of AI Agents — Capabilities, Limitations, and Implications for WAX." It should be honest about what is not working, not just what is impressive.

**Exit criteria:** You should be able to describe the current capability landscape accurately enough that a senior AI engineer would agree with your assessment. You should understand where WAX's ambitions are achievable with current technology and where they require either future technology or significant engineering mitigation.

**What should remain undecided:** Which agent framework, if any, WAX should adopt. Which model provider WAX should use. Whether WAX needs multiple models. The answer to any of these depends on subsequent stages.

**Risks:** The field moves fast. Research conducted today may be partially obsolete in six months. Build the research stage to be repeatable, not one-time.

**What can be researched in parallel:** The education mission research (Stage 7) does not depend on the agent landscape and can begin immediately. Stage 1 and Stage 7 are the two tracks that can run simultaneously from the start.

---

### STAGE 2 — RUNTIME AND ENVIRONMENT DESIGN PRINCIPLES
**Objective:** Understand what a "runtime for intelligence" means, what it must contain, and how existing systems have approached this problem.

**Why it comes at this point:** Stage 1 tells you what the AI can do. Stage 2 asks: what environment does the AI need in order to do it reliably, persistently, and securely? You cannot answer that question before answering Stage 1, because the environment must be designed around real AI capabilities, not imagined ones.

**What this means in plain English:** Think of this as the difference between a human being's capabilities (what they can think and do) and the environment in which they operate (the tools, spaces, systems, laws, and infrastructure available to them). WAX's "runtime" is the environment. Stage 2 is about understanding what a well-designed environment for an AI intelligence needs to contain.

**Why WAX cares:** The WAX philosophy makes a very specific claim: "infrastructure, not intelligence." The infrastructure — the runtime — should provide the conditions under which intelligence can operate, without encoding intelligence itself. That claim needs to be tested against real systems. What does a runtime actually contain? What must it provide? What happens if it provides too little? What happens if it encodes too many assumptions?

**What an engineer would call it:** Runtime design, agent execution environment, middleware architecture, platform architecture.

**Prerequisite knowledge:** Understanding of what happens when you call an LLM API: you send a request, you get a response, the model remembers nothing. Understanding of what "state" means: the information that describes the current situation of an ongoing process. Understanding that a long AI task necessarily involves state — where are we in the process, what has been tried, what has been learned — and that state must be stored somewhere external to the model.

**Research questions:**
- What is the minimum set of things a runtime must provide for an AI to do useful, persistent work?
- What existing systems serve as runtimes for AI? How do they make architectural decisions?
- What is the difference between a runtime, an orchestration layer, an agent framework, and an application? These terms are used loosely in the field — WAX needs its own precise definitions.
- How do existing runtimes handle state — the current situation of an ongoing task? Where does state live? How is it accessed? How is it updated?
- What is "durable execution" and why does it matter for long-running AI work?
- What is Temporal.io and why has it become a reference system for durable, reliable, long-running processes? What can WAX learn from it?
- What is an "event loop" and why is it a common architectural pattern for systems that need to respond to asynchronous inputs?
- What is the difference between a synchronous system (where each step waits for the previous one to complete) and an asynchronous system (where steps can be in progress simultaneously)?
- How do existing AI agent runtimes handle the case where an AI takes an action, the action fails, and the AI needs to decide what to do?
- What does "idempotency" mean and why is it essential for any system that might need to retry failed operations?

**What this means in plain English — Durable Execution:** Imagine you are asking the AI to do something that takes an hour. Halfway through, the server crashes. When the server comes back, does the AI know where it was? Does it start over from the beginning? Does it retry steps that already succeeded? "Durable execution" means the system is designed so that long-running processes survive failures and resume intelligently rather than restarting blindly. This is one of the hardest engineering problems in distributed systems and it is directly relevant to WAX's ambition of long-running objective pursuit.

**What this means in plain English — Idempotency:** If you send an email and the system crashes before it can record that the email was sent, when the system recovers, does it send the email again? Idempotency means that if an operation is performed multiple times due to retries, the result is the same as if it were performed once. Without idempotency, a retry can cause duplicate actions, double charges, or corrupted state.

**Technologies and systems to investigate:**
- Temporal.io (durable execution platform — extremely relevant to long-running AI tasks)
- Inngest (event-driven durable functions)
- Apache Kafka (event streaming — relevant to asynchronous state management)
- AWS Step Functions (state machine orchestration — relevant to multi-step process management)
- Microsoft Durable Functions (Azure's durable execution equivalent)
- LangGraph (state machine approach to agent orchestration)
- Prefect and Dagster (workflow orchestration — less AI-specific but relevant to reliability)
- Ray (distributed computing framework used by AI systems)
- Modal (cloud compute platform used for AI workloads)
- E2B (code execution sandboxes for AI)
- Composio (tool integration platform for agents)

**Key concepts to understand:**
- State machines: A way of modeling a system as a set of states and transitions between them. Every stage of a long task can be a state. Transitions happen when something is completed or when an input is received. State machines make the flow of a process explicit and auditable.
- Event sourcing: Instead of just storing the current state of something, store the complete history of events that led to that state. This makes it possible to replay history, audit what happened, and recover from failures by replaying events.
- Message queues: Systems that store messages until they can be processed. They decouple the sender from the receiver, which is important when you need reliable delivery even if the receiver is temporarily unavailable.
- Checkpointing: Saving the state of a process at regular intervals so that if it fails, you can restart from the most recent checkpoint rather than from the beginning.

**Research questions specific to WAX:**
- Given that WAX wants the AI to work on objectives that might span hours or days, how does the runtime track what has been accomplished?
- If a user leaves and comes back the next day, how does the runtime provide the AI with enough context to continue intelligently?
- How does the runtime expose to the AI what it can currently do? Does the AI discover tools dynamically, or are they pre-loaded?
- What happens when the AI takes an action in the external world — sends an email, makes an API call, executes code — and the result is ambiguous or the action partially fails?
- How does the runtime distinguish between an AI that is appropriately waiting for a long process to complete and an AI that is stuck in an infinite loop?

**Expected deliverable:** A Research Report titled "Runtime and Environment Architecture — What WAX's Intelligent Environment Must Contain." This document should include a conceptual diagram of the runtime's layers, an analysis of at least three existing runtime approaches, their tradeoffs, and a preliminary list of mechanisms WAX's runtime must include regardless of which specific technologies are chosen.

**Exit criteria:** You should be able to describe what WAX's runtime must do — not what technology it should use — in enough detail that an engineer could propose three different ways to implement it. If you can only describe the runtime in vague terms ("it manages state and tools"), you have not yet reached the necessary level of understanding.

**What should remain undecided:** The specific technologies used to implement the runtime. Whether WAX builds its own runtime or builds on top of an existing one. The specific data formats used to represent state.

---

### STAGE 3 — MEMORY, CONTINUITY, AND IDENTITY
**Objective:** Understand the full landscape of memory in AI systems: what types of memory exist, how they are implemented, what the tradeoffs are, and what WAX specifically needs.

**Why it comes at this point:** Memory is what converts a stateless model into a persistent intelligence. You cannot design WAX's memory system before understanding the runtime (Stage 2), because memory must be stored in and retrieved from the runtime. And you cannot design the runtime before understanding AI capabilities (Stage 1), because the runtime must serve real AI capabilities.

**What this means in plain English:** When you meet a person for the second time, they remember your name, what you talked about, and what they know about you. When you call an LLM API, it remembers nothing from any previous call. Everything the AI knows about who you are, what you have worked on together, what it has learned — all of that must be explicitly provided to the model on each API call, retrieved from storage. Memory in AI systems is not a feature of the model — it is an engineering system built around the model.

**Why WAX cares:** WAX wants the experience of a persistent relationship with an intelligence. The human brings an objective. The AI understands who they are. The AI knows what was worked on previously. The AI can continue rather than starting over. That experience — which is fundamental to WAX's philosophy — requires a sophisticated memory architecture. It is not something that happens automatically.

**What an engineer would call it:** Memory architecture, context management, RAG (Retrieval-Augmented Generation), episodic memory, semantic memory, working memory, vector database, context window management.

**Prerequisite knowledge — The Context Window Problem:** Every LLM can only see a limited amount of text at once. This is called the "context window." Older models had very short context windows — perhaps 4,000 tokens (roughly 3,000 words). Newer models have much longer windows — 100,000 to 1,000,000 tokens. But even 1,000,000 tokens is finite. A user's entire history with WAX over months of use would far exceed any context window. Therefore, memory must be selective: the system must decide what information to include in the context window for each interaction. This decision — what to remember, what to retrieve, what to forget — is the memory architecture problem.

**Types of memory in AI systems (explained):**

Working memory: The information currently in the AI's context window. This is what the model can "see" right now. It is temporary — when the conversation ends, it is gone unless explicitly saved.

Episodic memory: Records of specific past events. "On March 15th, this user asked me to help them write a biology essay on cell division. I provided an explanation of mitosis and meiosis. They said the explanation of meiosis was confusing. I rephrased it and they seemed satisfied." This is an episode — a specific event with context, participants, and outcome.

Semantic memory: General facts and knowledge about a person or situation that are not tied to a specific event. "This user is preparing for WAEC. They are strong in mathematics but find chemistry difficult. They prefer explanations that use real-world analogies. They study best in the evenings."

Procedural memory: Knowledge of how to do things. For AI, this might manifest as learned preferences about process — "this user prefers step-by-step breakdowns over summarized answers."

External memory: Information stored outside the model — in databases, files, vector stores — and retrieved when needed. This is what engineering systems must provide.

**Research questions:**
- What is Retrieval-Augmented Generation (RAG) and how does it enable an AI to access information stored outside its context window?
- What is a "vector database" and how is it different from a traditional relational database? What makes it useful for memory retrieval?
- How do you decide what information is worth saving to long-term memory and what should be discarded?
- What is "context compression" or "summarization" — reducing a long conversation to a shorter representation that preserves the most important information?
- How do you handle conflicting memories — what if the AI has stored incorrect information about a user and the user now says something different?
- What does "memory consolidation" mean in the context of AI systems, and how do biological memory consolidation mechanisms (from neuroscience) inform AI memory design?
- How do existing systems like ChatGPT (with memory), Claude Projects, and similar approaches handle persistent memory? What are their limitations?
- What is the risk of a memory system that is too aggressive — one that retains too much and uses it inappropriately?
- How should memory work across interfaces? If a user interacts with WAX via WhatsApp and then via web, the memory should be the same. What architecture makes that possible?
- What is the WAX Foundation document's hypothesis about a "smaller supporting model" periodically deciding what to retain? Has this approach been tried? What are its implications?

**Technologies and systems to investigate:**
- Pinecone, Weaviate, Qdrant, Chroma (vector databases for semantic similarity retrieval)
- PostgreSQL with pgvector extension (combining relational storage with vector search)
- mem0 (open-source memory layer for AI agents — directly relevant)
- Zep (long-term memory for AI applications)
- LangChain's memory modules (various approaches to memory, good for comparison)
- MemGPT / Letta (research system for infinite-context LLMs through memory management)
- OpenAI's Assistants API memory (their approach to persistent threads)
- Claude Projects (Anthropic's approach to persistent context)
- Microsoft Recall (controversial product but architecturally relevant — records and retrieves episodic information)

**Key papers:**
- "MemGPT: Towards LLMs as Operating Systems" (Packer et al., 2023) — highly relevant
- "Generative Agents: Interactive Simulacra of Human Behavior" (Park et al., 2023) — demonstrates episodic memory in AI agents
- "A Survey on Memory Mechanisms for Large Language Model based Agents" (Zhong et al., 2024)
- "Cognitive Architectures for Language Agents" (Sumers et al., 2023)

**WAX-specific research question:** The Foundation document suggests that a secondary model might monitor interactions and decide what to save. This is an interesting hypothesis. Research whether this "memory scribe" pattern exists in the literature, what its advantages are over having the primary model manage its own memory, and what its failure modes might be.

**Expected deliverable:** A Research Report titled "Memory Architecture for WAX — Types, Mechanisms, Tradeoffs, and Open Questions." It should include a comparison of at least four memory approaches, an analysis of what WAX specifically needs, and a preliminary memory taxonomy (the different categories of information WAX should store and retrieve).

**Exit criteria:** You should be able to describe, in concrete terms, what happens in WAX when a user returns after a week's absence — what information is retrieved, from where, in what form, and how it is used to reconstruct continuity. If you cannot describe this process concretely, the memory architecture is not yet understood.

**What should remain undecided:** The specific database technology. The specific retrieval mechanism. The exact schema for storing memories. Whether WAX uses one memory store or several.

---

### STAGE 4 — CAPABILITY ARCHITECTURE AND DYNAMIC TOOL SYSTEMS
**Objective:** Understand how an AI system can discover, acquire, compose, and use capabilities — including capabilities that were not known when the system was built.

**Why it comes at this point:** This stage depends on Stages 1, 2, and 3. You need to understand what AI can do (Stage 1) before designing capability systems. You need to understand the runtime (Stage 2) because capabilities are managed and mediated by the runtime. You need to understand memory (Stage 3) because an AI that has learned about a capability previously should be able to retrieve that knowledge.

**What this means in plain English:** A "capability" in WAX's context is anything the AI can use to act in the world — a tool, a service, a function, an integration, an external API, an execution environment. A static capability system is one where the capabilities are fixed at build time — the AI can only use exactly the tools it was given when the system was deployed. A dynamic capability system is one where capabilities can be discovered, added, removed, or modified at runtime — the AI might learn that a new tool exists, evaluate whether it is appropriate to use, and use it, all without the builders having anticipated that specific tool.

**Why WAX cares:** The WAX philosophy explicitly rejects a "closed catalogue" of capabilities. The school analogy makes this concrete: the teacher should not be born knowing only the tools that existed when the school was built. If a new tool becomes available — a new kind of marker, a new textbook, a new technology — the teacher should be able to reason about it and use it.

**What an engineer would call it:** Tool use, capability discovery, service composition, plugin systems, MCP (Model Context Protocol), function calling, OpenAPI integration, service mesh, capability-based security.

**Prerequisite knowledge — APIs and Tool Use:** An API (Application Programming Interface) is a defined way for one piece of software to request services from another. When a model "uses a tool," what is actually happening is: the model produces a structured output that specifies what tool to call and with what parameters. The runtime intercepts that output, calls the actual function or API, gets a result, and includes that result in the model's next context. The model never directly touches the external service — the runtime mediates all access.

**Research questions:**
- What is the Model Context Protocol (MCP) introduced by Anthropic? How does it attempt to standardize how AI systems discover and use external tools? What are its limitations?
- What is OpenAPI and how has it been used to allow AI systems to dynamically understand and use web APIs without being explicitly programmed to do so?
- What is the difference between "tool selection" (choosing from a fixed list) and "tool discovery" (finding tools that were not previously known)?
- How do existing systems handle tool composition — using the output of one tool as the input to another?
- What is "capability-based security"? How does it differ from traditional access control models? Why might it be relevant to WAX?
- How does an AI determine that a capability gap exists — that it cannot currently do something it needs to do?
- What does it mean to "provision" a capability? How would WAX add a new capability at runtime without redeploying the entire system?
- What is the risk of an AI that can dynamically acquire new capabilities? How do you prevent capability acquisition from becoming privilege escalation?
- How do existing systems like Zapier, Make (formerly Integromat), n8n, or IFTTT handle service composition? What can WAX learn from these, even though they are human-driven rather than AI-driven?
- What does "tool reliability" mean — some tools fail, some are slow, some return malformed data? How does an AI handle tool failures gracefully?

**What this means in plain English — Capability-Based Security:** Traditional security systems say "this user is an admin, so they can do anything." Capability-based security says "this agent has been given the capability to read files in this specific directory, and cannot do anything else, even if it somehow acquires admin credentials." For WAX, this is the difference between an AI that has been granted broad permissions and one that has only the specific capabilities needed for the current task. The latter is dramatically safer.

**Technologies and systems to investigate:**
- Anthropic's Model Context Protocol (MCP) — this is directly relevant
- LangChain tools and tool kits
- OpenAI function calling specification
- Composio (tool integration platform)
- Zapier's AI integration (for comparison)
- OpenAPI/Swagger (standard for describing web APIs)
- WASM (WebAssembly) as a capability sandboxing mechanism
- AWS Lambda and similar function-as-a-service — relevant to how capability invocations might be isolated
- The capability security model from the E/OS research community

**Key concepts to understand:**
- The principle of least privilege: an AI should be given only the minimum capabilities necessary for the current task, and those capabilities should be revoked when the task is complete.
- Capability revocation: the ability to take away a capability that was previously granted.
- Sandboxing: running capability invocations in an isolated environment so that a malicious or buggy capability cannot affect the rest of the system.

**Research questions specific to WAX:**
- How does WAX present its capability landscape to the AI? Does the AI receive a complete list of all capabilities at the start of every interaction? Does it query for capabilities as needed? Does it reason about what capabilities might exist even if they are not listed?
- How does WAX handle the case where a user asks for something that requires an external service not yet integrated? Does the AI acknowledge the gap? Does it have a mechanism to request that the capability be added?
- How does WAX distinguish between capabilities that are built-in (always available), capabilities that are provisioned per-user (available to this user because they have connected a specific service), and capabilities that are provisioned per-session (available for this specific task)?

**Expected deliverable:** A Research Report titled "Capability Architecture — How WAX's Intelligence Discovers, Uses, and Acquires Tools." It should include a taxonomy of capability types, an analysis of existing capability systems, and a preliminary design for how WAX exposes capabilities to the AI.

**Exit criteria:** You should be able to describe, concretely, how WAX would handle the following scenario: A user asks for help analyzing a spreadsheet. WAX has no spreadsheet tool. The AI recognizes the gap. What happens next — and how is each step of that process managed by the runtime? If you cannot answer this concretely, capability architecture is not yet understood.

---

### STAGE 5 — SECURITY, AUTHORIZATION, AND TRUST ARCHITECTURE
**Objective:** Understand the security requirements for an open-world intelligent environment and research the mechanisms that make strong security compatible with significant AI autonomy.

**Why it comes at this point:** Security must be understood before any capability is designed or implemented. This stage should not wait until the system is being built — security requirements that are added after architecture decisions are made are always weaker than security requirements that shape architecture from the beginning.

**What this means in plain English:** The more autonomy you give the AI, the more dangerous it becomes if something goes wrong. An AI that can only answer questions cannot be manipulated into doing much harm. An AI that can send emails, execute code, access external services, store information, and manage files on behalf of users is significantly more dangerous if it is manipulated, compromised, or simply makes wrong decisions. Security is not an add-on to WAX. It is the thing that makes WAX's level of autonomy acceptable.

**Why WAX cares:** The WAX document states explicitly: "The AI should have agency but not sovereignty." That is the security mission statement. The AI can propose actions. The environment decides whether those actions are permitted. The AI cannot override the environment's decisions. Making that principle technically real and resistant to attack is the security architecture problem.

**What an engineer would call it:** Authorization, authentication, access control, identity management, secret management, audit logging, threat modeling, prompt injection defense, sandboxing, privilege separation, zero trust architecture.

**Critical distinction to understand — Authentication vs. Authorization:**
Authentication: proving who you are ("I am this specific user").
Authorization: determining what you are allowed to do ("This specific user is allowed to read these files but not delete them").
Both are necessary. Authentication without authorization means everyone who can log in can do everything. Authorization without authentication means you cannot tell whose actions you are controlling.

**Prerequisite knowledge — The AI-Specific Security Problem:** Traditional software security assumes a deterministic system: if you call a function with specific inputs, you get a specific output, and you can predict what it will do. AI systems are fundamentally non-deterministic: the same prompt can produce different outputs. This means traditional security testing — testing every possible input-output combination — is not feasible. AI systems can be manipulated through carefully crafted inputs in ways that normal software cannot. This is the core of why AI security is a distinct field from traditional software security.

**What this means in plain English — Prompt Injection:** If the AI can read external content — web pages, emails, documents — an attacker can embed instructions in that content that the AI reads and follows as if they were instructions from the legitimate user. This is called "prompt injection" and it is one of the most serious security threats for AI systems that interact with the external world. For WAX, which wants to reach out to external services and read external content, prompt injection must be a first-class design concern, not an afterthought.

**Research questions:**
- What are the primary threat models for an AI system with significant autonomy? That is, who might attack it, how, and with what goals?
- What is prompt injection, what are the known variants (direct injection, indirect injection, multi-step injection), and what defenses have been proposed and evaluated?
- What is "confused deputy" — a security vulnerability where a system with authority to perform an action is tricked into performing it on behalf of an attacker? How does this apply to AI agents?
- How should WAX handle user credentials for external services? If a user connects their Gmail account, how does WAX store, access, and use those credentials without exposing them inappropriately?
- What is OAuth 2.0 and why is it the standard mechanism for delegated authorization? How would WAX use it to allow users to connect external services?
- What is secret management? How do systems like HashiCorp Vault, AWS Secrets Manager, or similar tools protect sensitive credentials?
- What does "audit logging" mean and why is it essential? For every action the AI takes, who authorized it, what was the action, what was the result, and when did it happen — and can this record be tampered with?
- What is "zero trust architecture"? It means no entity — not even the AI system itself — is trusted by default. Every request must be authenticated and authorized regardless of where it comes from.
- How do you isolate the execution of user code or AI-generated code so that it cannot affect other users or the system itself?
- What is the minimum level of AI autonomy that makes WAX useful, and how does that level of autonomy constrain the security architecture?
- What does "revocation" mean in the context of AI capabilities? If you discover that the AI was manipulated into performing unauthorized actions, can you undo those actions? Can you revoke the capabilities that allowed them?
- What specific security requirements arise from WAX's education mission — serving potentially young users?

**Technologies and systems to investigate:**
- OAuth 2.0 and OpenID Connect (standards for delegated authorization and identity)
- HashiCorp Vault (secret management)
- AWS IAM, Google Cloud IAM (identity and access management in cloud systems)
- OWASP Top 10 for LLM Applications (the field's best current catalog of AI-specific security threats)
- Garak (LLM security testing tool)
- Lakera Guard (prompt injection detection)
- ProtectAI (AI security company)
- Research on indirect prompt injection from ETH Zurich and other groups
- Capability-based security from the object-capability research community (Mark Miller's work)

**Key papers and resources:**
- "Indirect Prompt Injection Attacks on Large Language Model-Integrated Applications" (Greshake et al., 2023) — essential reading
- OWASP LLM Top 10 (2024 version)
- "Evaluating the Susceptibility of Pre-Trained Language Models via Handcrafted Adversarial Examples" — for understanding adversarial robustness
- NIST AI Risk Management Framework
- Anthropic's documentation on AI safety (for the philosophical foundation)

**WAX-specific security considerations:**
WAX will be used by students, potentially including minors. This creates regulatory requirements (COPPA in the US, similar frameworks elsewhere) and ethical requirements that go beyond typical security considerations. The system must be designed to be appropriate for young users, which affects what capabilities are exposed, what content is accessible, and how user data is handled.

**Expected deliverable:** A comprehensive Security Architecture Research Report that includes: a threat model for WAX, a catalog of AI-specific security threats and proposed defenses, an analysis of authorization models suitable for WAX, recommendations for the security properties the runtime must enforce, and a list of security considerations that must be addressed before the capability architecture is finalized.

**Exit criteria:** You should be able to answer the question "What happens if an attacker embeds malicious instructions in a web page that the WAX AI reads?" with a concrete description of the defenses in place and the residual risks. If you cannot answer that question, security architecture is not yet understood.

**What should remain undecided:** Specific security technology choices. The exact authorization policy for each capability. The specific audit log format.

---

### STAGE 6 — EXECUTION, ISOLATION, AND COMPUTE ENVIRONMENTS
**Objective:** Understand the mechanisms by which WAX can run code, interact with browsers, produce files, and generally act in the computational world, in an isolated and resource-controlled way.

**Why it comes at this point:** Execution environments depend on capability architecture (Stage 4) because execution is a class of capability, and on security (Stage 5) because isolated execution is one of the primary security mechanisms.

**What this means in plain English:** At some point, an AI working on a user's behalf will need to actually do things that go beyond producing text — run code, open a web page, create a file, call an API, install a package. When that happens, something needs to run that code in a controlled way so that: (a) it cannot affect other users or the system, (b) it cannot consume unlimited resources, (c) its output can be captured and given back to the AI, and (d) if it does something destructive, the damage is contained. This controlled execution environment is a "sandbox."

**Why WAX cares:** WAX's vision of long-running objective pursuit, dynamic capability acquisition, and richer execution environments (the Foundation document's mention of "temporary richer environments") all require execution infrastructure. An AI that cannot run code, interact with browsers, or produce complex outputs is severely limited in the range of objectives it can pursue.

**What an engineer would call it:** Sandboxing, containerization, microVMs, code execution environments, browser automation, infrastructure-as-code, compute scheduling.

**What this means in plain English — Containers and microVMs:** A "container" (like Docker) is a lightweight way of isolating a running process from the rest of the system. It shares the host operating system but has its own file system, network, and process space. A "microVM" (like Firecracker, developed by AWS) is a even more isolated execution environment — essentially a very lightweight virtual machine that starts up in milliseconds. For AI code execution, microVMs provide stronger isolation than containers because they have their own kernel, making it much harder for malicious code to escape.

**Research questions:**
- What is Docker and how does containerization provide isolation? What are its limitations as a security boundary?
- What is Firecracker and why did AWS build it? How do microVMs provide stronger isolation than containers?
- What is WebAssembly (WASM) and why is it gaining traction as a sandboxing mechanism? What are its limitations?
- What is E2B and how does it provide code execution sandboxes for AI applications?
- What is browser automation? What is Playwright, Puppeteer, or Selenium, and how can an AI control a browser to interact with the web?
- What are the resource implications of running isolated execution environments at scale? How does AWS Lambda, Google Cloud Run, or similar serverless platforms manage resource isolation and billing?
- What is the "cold start" problem in serverless computing — the delay that occurs when a new execution environment is spun up — and how does it affect the user experience?
- What is "ephemeral" vs. "persistent" compute? WAX's Foundation document mentions "temporary execution environments" — how would this work technically?
- How do existing AI coding tools (like GitHub Copilot workspace, Devin, Claude Code) handle code execution? What isolation do they use?
- What is the resource cost of isolated execution at the scale WAX might eventually serve (thousands of concurrent users)?

**Technologies and systems to investigate:**
- Docker and container runtimes (containerd, runc)
- Firecracker (microVM, used by AWS Lambda internally)
- gVisor (Google's sandbox kernel for containers)
- E2B (purpose-built code execution sandboxes for AI — highly relevant)
- Modal (cloud platform for isolated compute, popular with AI applications)
- Fly.io (application deployment with good isolation story)
- Playwright and Puppeteer (browser automation — relevant to WAX's browser interaction ambitions)
- WebAssembly (WASM) and WASI (WebAssembly System Interface)
- Nix/NixOS (reproducible environments — less directly relevant but important for understanding)
- GitHub Codespaces (example of ephemeral development environments)

**Key research questions specific to WAX:**
- The Foundation document mentions that WAX might "provision or expose an appropriate environment for the user." What does this mean technically? Is it a container? A microVM? A browser session? All of the above?
- How does WAX associate a temporary execution environment with a specific user's ongoing work?
- How does the AI access the results of code execution? What is the interface between the execution environment and the AI runtime?
- What happens when a user's execution environment is idle — does it remain running (costly) or does it shut down (cold start problem)?

**Expected deliverable:** A Research Report titled "Execution Environments for WAX — Sandboxing, Isolation, and Compute Architecture." It should compare several isolation approaches, analyze their tradeoffs for WAX's specific needs, and establish preliminary requirements for WAX's execution infrastructure.

**Exit criteria:** You should be able to describe, concretely, what happens when a user asks WAX to write and run a Python script — where the code runs, how it is isolated, how the output is captured, how the AI receives that output, and what prevents that code from accessing other users' data.

---

### STAGE 7 — EDUCATION MISSION DEPTH RESEARCH
**Objective:** Research what is actually known about AI-assisted learning, what makes AI tutoring effective versus ineffective, and how WAX can serve Nigerian students preparing for major examinations at the highest possible level.

**Why it can start early:** This research does not depend on architectural decisions. It can begin in parallel with Stages 1-6 and should be contributed to continuously throughout the project.

**Why it matters now:** Education is WAX's first mission, not its eventual mission. Every architectural decision should be compatible with serving students extraordinarily well. Without deep research into educational effectiveness, WAX might produce a technically impressive system that is pedagogically mediocre.

**What this means in plain English:** The goal of education is not to give students answers. It is to help them understand things in ways they did not understand before. An AI that quickly produces correct answers is not automatically a good tutor. A good tutor understands where the student is confused, provides explanations appropriate to that specific confusion, uses examples and analogies that resonate with the student's existing knowledge, and helps the student develop the ability to reason independently rather than dependently. Research must establish what the evidence says about how AI can do these things well.

**Research questions:**
- What is the research evidence for "Socratic method" approaches in AI tutoring — asking the student questions rather than providing answers? What outcomes do they produce?
- What is "Bloom's Taxonomy" and how does it inform the design of educational AI? What kinds of understanding go beyond recall?
- What is "spaced repetition" and what is the evidence for its effectiveness in knowledge retention? How have existing systems (Anki, Duolingo) implemented it?
- What is "metacognition" and why does research suggest it is one of the most important factors in learning? How can an AI support metacognitive development?
- What is the "Worked Example Effect" and what does it suggest about how AI should present solutions?
- What is the Khanmigo project (Khan Academy's AI tutor) and what has it demonstrated about AI tutoring in practice?
- What specific knowledge gaps are common among Nigerian students preparing for WAEC, NECO, and JAMB? What subjects are hardest? What teaching approaches have proven effective?
- What are the pedagogical risks of AI tutoring — ways it might help students pass exams without building genuine understanding?
- How should WAX adapt its explanations to a student's demonstrated level? What is "adaptive difficulty" and how have systems implemented it?
- What is the role of motivation and emotional state in learning? How can an AI recognize and respond to student frustration, disengagement, or discouragement?
- How should WAX handle students who want answers without engaging with understanding? What is the appropriate approach?

**Key resources and systems to investigate:**
- Carnegie Learning (evidence-based AI tutoring company with decades of research)
- Khan Academy and Khanmigo
- Duolingo (extremely evidence-based language learning — methodology is transferable)
- Coursera and edX research on online learning effectiveness
- Bloom's Taxonomy (foundational educational research)
- Vygotsky's Zone of Proximal Development (foundational learning theory)
- Cognitive Load Theory (Sweller — important for how to structure explanations)
- The "Intelligent Tutoring Systems" research field (decades of work on AI tutoring before LLMs)
- WAEC and JAMB past examination papers (direct research into what students need to learn)
- Research on African educational contexts and effective teaching in resource-constrained environments

**Expected deliverable:** An Education Research Report covering: principles of effective AI-assisted learning, specific implications for WAX's design, analysis of existing AI tutoring systems, and a pedagogical philosophy document that defines how WAX should approach the education mission.

**Exit criteria:** You should be able to describe what WAX should do differently from a simple question-answering AI in order to genuinely help a student understand, not just answer. If the difference cannot be described clearly, the educational research has not gone deep enough.

---

### STAGE 8 — MULTI-MODEL AND MODEL-INDEPENDENCE RESEARCH
**Objective:** Understand how WAX can be designed so that it is not permanently dependent on any single AI model, provider, or capability profile.

**Why it comes at this point:** Model-independence requires understanding what models do (Stage 1), how the runtime mediates their use (Stage 2), and what capabilities they access (Stage 4). Those earlier stages must be understood before you can design a system that treats models as interchangeable resources.

**What this means in plain English:** Today's best AI model might not be the best model in two years. An organization that builds its entire system around one specific model and one specific provider is making a very risky bet. If that provider changes their pricing, changes their model, goes out of business, or is outcompeted, the dependent system has a serious problem. WAX should be designed so that the AI model is a resource it uses, not an identity that defines it.

**Why WAX cares:** The Foundation document states: "The model should be replaceable." This is not just about avoiding vendor lock-in. It is about WAX's identity. WAX is the environment and the experience. The intelligence is what makes the environment useful. But the specific model providing the intelligence can change as better models become available, as cost considerations change, or as different models prove better for different tasks.

**What an engineer would call it:** Model abstraction, model routing, LLM gateway, multi-model orchestration, provider independence.

**Research questions:**
- What is an "LLM gateway" or "AI gateway" and how does it abstract the underlying model from the application?
- What are the significant differences between major model providers that would affect WAX's architecture? (Not which is "better" — what architectural differences matter?)
- What is "model routing" — dynamically selecting different models for different tasks based on capabilities, cost, or latency?
- How do existing systems like LiteLLM or similar gateways provide model abstraction?
- What are the capabilities that are standard across all major models (basic reasoning, instruction following) and what capabilities are provider-specific (tool use format, context length, multi-modal input)?
- Can WAX's abstraction layer be designed so that adding a new model provider requires only adding an adapter, not changing the core runtime?
- What does "model-specific behavior" mean — ways that different models produce different outputs for the same prompt — and how can the runtime account for this?
- Is multi-model orchestration (routing different subtasks to different specialized models) currently reliable enough to depend on in production? What evidence exists?
- What are the latency and cost implications of model routing?

**Technologies and systems to investigate:**
- LiteLLM (open-source LLM gateway with broad provider support)
- OpenRouter (model routing and provider abstraction service)
- Portkey (AI gateway)
- Helicone (observability layer for LLM calls)
- AWS Bedrock (multi-model platform from AWS)
- Google Vertex AI (multi-model platform from Google)
- LangChain's model abstraction (for comparison)

**Expected deliverable:** A Research Report titled "Model Independence Architecture — How WAX Avoids Permanent Dependency on Any Single AI Provider." It should include an analysis of the abstraction layer required, comparison of existing gateway solutions, and preliminary recommendations for WAX's model architecture.

**Exit criteria:** You should be able to describe how WAX would switch from one model provider to another — what changes in the system, what stays the same, and what the risks are. If the answer is "essentially nothing changes" in the runtime, the model abstraction is good. If the answer is "everything changes," it needs more work.

---

### STAGE 9 — LONG-RUNNING OBJECTIVES AND RESUMABILITY
**Objective:** Understand the engineering mechanisms required for an AI to work on objectives that span many steps, significant time, and potential interruptions.

**Why it comes at this point:** This stage synthesizes insights from the runtime (Stage 2), memory (Stage 3), capability (Stage 4), and execution (Stage 6) stages. It is a higher-level concern that sits on top of those foundations.

**What this means in plain English:** Most AI systems work in a request-response pattern: you send a message, the AI responds, done. WAX wants to support a much more complex pattern: a user sets an objective that might take hours or days, the AI works on it step by step, the work might be interrupted by failures or the user's absence, and when the user returns or the system recovers, the work resumes intelligently rather than starting over.

**Why WAX cares:** This is one of the most distinctive and technically challenging parts of the WAX vision. It is also directly relevant to the education mission: a student might start working through a concept over multiple sessions, and WAX should be able to pick up exactly where they left off, understanding what has been mastered, what remains confusing, and what the next appropriate step is.

**What an engineer would call it:** Durable workflows, workflow orchestration, checkpoint-restart, saga patterns, event-driven architecture, long-running transactions.

**What this means in plain English — The Saga Pattern:** In distributed systems, a "saga" is a sequence of local transactions where each transaction updates one service and publishes an event to trigger the next transaction. If one transaction fails, the saga executes compensating transactions to undo the work already done. For WAX, a "saga" might be an AI objective that spans multiple steps and services — the saga pattern ensures that if any step fails, the system can either retry or cleanly undo the previous steps rather than leaving things in an inconsistent state.

**Research questions:**
- What is Temporal.io's execution model? How does it guarantee that a workflow will complete even if the underlying infrastructure fails? How does this apply to AI agent workflows?
- What is the difference between "checkpoint-restart" (saving the complete state of a process and restarting from that state) and "event sourcing" (replaying all events that led to the current state)?
- How do existing AI agents handle the case where a multi-step task is interrupted? What do frameworks like LangGraph and AutoGen do? What are their limitations?
- What is "idempotency" and how do you design operations that can be safely retried? This is especially important for operations with real-world side effects (sending messages, making purchases, calling APIs that charge per call).
- How does the system present the status of a long-running task to the user? What level of transparency is appropriate?
- What happens when the AI's plan — the sequence of steps it was intending to take — is invalidated by new information or a changed situation? How does the system recognize this and adapt?
- What is the distinction between tasks that are waiting (paused, expecting input or time to pass) and tasks that are stuck (in an error state or infinite loop)? How does the runtime tell the difference?
- What are the storage and compute costs of maintaining the state of thousands of concurrent long-running tasks?

**Technologies to investigate:**
- Temporal.io (the most directly relevant system — study in depth)
- Conductor (Netflix's workflow orchestration system)
- Apache Airflow (workflow management platform — different use case but architecturally relevant)
- LangGraph (explicitly designed for multi-step, stateful AI agents)
- AWS Step Functions (state machine service)
- Prefect and Dagster (modern workflow orchestration)
- Inngest (durable functions for serverless environments)

**Expected deliverable:** A Research Report titled "Long-Running Objectives in WAX — Architecture for Durable, Resumable AI Work." It should include analysis of the Temporal.io model, evaluation of whether WAX should build on an existing orchestration platform or build its own, and preliminary requirements for WAX's workflow system.

**Exit criteria:** You should be able to describe, concretely, what happens when a user asks WAX to research a topic, produce a document, and revise it based on feedback — a multi-step task that might take hours — and then the user loses internet access for two days. When the user returns, what does WAX do? How does this work technically?

---

### STAGE 10 — OBSERVABILITY, AUDIT, AND OPERATIONAL INTELLIGENCE
**Objective:** Understand how WAX monitors itself, records what happens, detects problems, and enables the operators and users to understand what the AI is doing.

**Why it comes at this point:** Observability is a foundational requirement that cannot be added after the fact. It must be designed into the system from the beginning. It depends on the runtime (Stage 2) because observability instruments the runtime, and on security (Stage 5) because audit logs are a security mechanism.

**What this means in plain English:** When an AI system is doing complex work on your behalf, you need to be able to understand what it did, why it did it, whether it made mistakes, and whether it followed the rules. This is observability — the ability to understand the internal state of a complex system by examining its outputs and records. Without observability, a complex AI system is a black box. When something goes wrong, you have no idea why.

**Why WAX cares:** An AI with significant autonomy that is completely unobservable is dangerous regardless of how well it was designed. Users need to trust WAX. Trust requires transparency. The operators building WAX need to understand what the system is doing in order to improve it. Security requires complete audit logs. All of this is observability.

**What an engineer would call it:** Observability, tracing, logging, metrics, alerting, audit logging, explainability.

**The Three Pillars of Observability:**
Logs: Records of what happened. "At 14:32:07, the AI called the Google Search tool with the query 'WAEC biology syllabus.' The tool returned the following results."
Metrics: Quantitative measurements. "Average task completion time: 3.2 minutes. Tool call failure rate: 2.3%. Cost per user session: $0.04."
Traces: The complete path of a request through the system. "This user message went through the ingestion layer, was processed by the memory retrieval system, passed to the model, which called three tools, produced a response, which was stored in memory and sent to the user." Traces show you the entire chain of cause and effect.

**Research questions:**
- What is distributed tracing and why is it essential for a system that has many components each doing a small piece of the work?
- What is OpenTelemetry and why has it become the standard for distributed observability?
- How do existing AI agent platforms provide observability into what the AI is doing? What are the current tools (Langsmith, Helicone, Arize Phoenix, etc.)?
- What is the difference between logging for debugging (helping developers understand failures) and audit logging for accountability (creating an immutable record that proves what actions were taken)?
- How do you provide users with an understandable view of what the AI is doing on their behalf? What is the appropriate level of detail — enough to build trust without overwhelming the user with technical detail?
- What are the privacy implications of observability? If WAX logs everything the AI does, that log contains information about everything the user does. How is that protected?
- What is anomaly detection and how can it be used to detect when an AI is behaving in unusual ways that might indicate a security incident or a model failure?

**Technologies to investigate:**
- OpenTelemetry (the observability standard)
- Langsmith (LangChain's observability platform for AI applications)
- Helicone (observability and analytics for LLM calls)
- Arize Phoenix (AI observability)
- Grafana and Prometheus (metrics and dashboarding)
- DataDog (general observability platform used by many production AI systems)
- Honeycomb (tracing-first observability platform)

**Expected deliverable:** An Observability Architecture Research Report that establishes what WAX must monitor, what must be logged, how traces should be structured for an AI agent system, and preliminary recommendations for WAX's observability stack.

---

### STAGE 11 — SCALE, DISTRIBUTION, AND GLOBAL INFRASTRUCTURE
**Objective:** Understand what WAX's infrastructure would need to look like at genuine scale — serving large numbers of concurrent users with persistent, reliable, low-latency intelligent service.

**Why it comes late in the sequence:** You should not design for scale before you understand what you are scaling. The architecture decisions from Stages 1-10 will determine what the scaling requirements are. Designing for scale too early is a common and very expensive mistake.

**What this means in plain English:** When one user uses WAX, the technical requirements are one thing. When a hundred thousand students in Nigeria simultaneously use WAX to prepare for WAEC, the technical requirements are completely different. Stage 11 is about understanding what that transition requires — not necessarily building it immediately, but understanding it so that the early architecture does not accidentally make the transition impossible.

**Research questions:**
- What are the compute, storage, and network requirements for serving 1,000, 10,000, 100,000, and 1,000,000 concurrent users of an AI system?
- What does horizontal scaling mean for an AI system — specifically one with persistent state? (Scaling stateless systems is relatively straightforward. Scaling stateful systems is much harder.)
- What is "database sharding" and why might WAX eventually need it?
- What is CDN (Content Delivery Network) and how does proximity to users affect latency?
- What are the implications of regional data residency requirements? Some countries require that user data be stored in that country. How does this affect a global system?
- What would it cost to serve a large user base at scale? What are the major cost drivers?
- How does the cost of AI inference (calling the model) factor into the economics of a scaled system?
- What architectural decisions made early will be the hardest to undo at scale?

**Technologies to investigate:**
- AWS, Google Cloud, Azure (major cloud providers — understand their offerings for AI workloads)
- Kubernetes (container orchestration at scale)
- Cloudflare Workers (edge computing — relevant to low-latency global deployment)
- CockroachDB, PlanetScale, Neon (distributed relational databases)
- Redis and alternatives (in-memory caching and state storage)
- Distributed message queues (Kafka, Pulsar, SQS)
- Vector database scaling (Pinecone, Weaviate at scale)

**Expected deliverable:** A Scaling Architecture Research Report that describes the infrastructure trajectory WAX needs — from minimal viable prototype through to global scale — and identifies the architectural decisions that must be made early to enable that trajectory.

---

## PART IV — FOUNDATIONAL PROTOTYPE

### The Right Prototype for WAX

The foundational prototype for WAX should not demonstrate WhatsApp integration, an educational syllabus, or a specific subject. It should demonstrate the single deepest idea in the WAX philosophy: that an AI can operate inside an environment it did not pre-specify, use capabilities it discovers at runtime, maintain continuity across sessions, and pursue an objective across multiple steps — while the environment enforces security boundaries the AI cannot override.

### The Prototype Definition

Call this the "Open Objective Loop" prototype.

**What it does:**

It takes an unspecified objective from a user (not a pre-defined task type, not a tutoring request, not a specific workflow). The AI reasons about the objective, determines what capabilities are available in the environment, uses them to make progress, stores the results in memory, and continues or resumes across session boundaries. The environment keeps an audit trail of everything the AI does. The AI cannot exceed the permissions it has been given, even if it reasons that exceeding them would help accomplish the objective.

**Specifically:** The prototype should have a very small set of real capabilities (perhaps: web search, text file creation, a calculator function, and a simple note-taking function). The user states an objective that might require multiple of these capabilities in an unspecified sequence. The AI reasons about how to use them. The session can be interrupted and resumed. The audit trail is visible.

**What this validates:**

It validates the relationship between the AI and the runtime — that the runtime can expose capabilities without the AI being hardcoded with them. It validates that capability discovery works. It validates that memory and continuity are real. It validates that security boundaries hold when the AI reasons about exceeding them. It validates that the system can handle an unspecified objective without a pre-programmed workflow.

**Why it is not a WhatsApp prototype:**

A WhatsApp prototype validates integration with WhatsApp. That is not the deep idea. The deep idea is the AI-runtime relationship. That relationship can be validated with a simple terminal interface or minimal web UI, which has no technical overhead and eliminates distraction.

**Why it is not a tutoring prototype:**

A tutoring prototype validates educational content delivery. That is important but it is not the architectural core. The tutoring mission can be built on top of a working AI-runtime relationship. It cannot substitute for one.

**Approximate scope of the prototype:**

The prototype should be buildable in a short time with minimal resources. It needs: one LLM API (any major provider), a small database for storing memory, a small set of hardcoded capabilities (not dynamically acquired — that is later), a basic session management layer, and an audit log. The point is to validate the pattern, not to build production infrastructure.

**What success looks like:**

A user can give an unspecified objective that requires sequencing multiple capabilities. The AI produces a coherent multi-step plan, executes it, stores what it learned, and when the session is restarted, continues intelligently. The audit log shows every step. A capability the AI does not have access to is requested and denied by the runtime. The AI reasons about the denial and finds an alternative route or reports the gap honestly.

---

## PART V — PARALLEL RESEARCH

The following research can proceed simultaneously without waiting for earlier stages to complete:

**Parallel Track A — Education Mission (can start immediately):** Research into pedagogical effectiveness of AI tutoring, WAEC/NECO/JAMB curriculum research, analysis of existing educational AI systems, and development of WAX's pedagogical philosophy. None of this depends on architecture.

**Parallel Track B — Security landscape (can start with Stage 1):** The security threat landscape for AI systems changes continuously. Security research can proceed in parallel with all architecture stages and should feed into each of them.

**Parallel Track C — Competitive and reference system analysis:** Deep dives into the most relevant existing systems (Temporal.io, E2B, MemGPT, LangGraph, MCP) can proceed in parallel. Each of these is an independent research topic.

**Parallel Track D — Infrastructure economics:** Understanding what things cost at different scales, who the major cloud providers are, and what pricing models exist for AI inference, storage, and compute. This informs later decisions but does not block them.

**Parallel Track E — Regulatory landscape for Nigeria:** NDPR (Nigeria Data Protection Regulation), educational regulations in Nigeria, and any regulations specific to AI systems serving young users. This is background research that should be ongoing.

---

## PART VI — WHAT YOU PERSONALLY NEED TO LEARN

This is a founder-level learning roadmap. The goal is not to make you a specialist in every area — it is to make you knowledgeable enough to understand what your research agents and engineers are telling you, evaluate their proposals, and make architectural decisions.

**Priority 1 — How LLMs actually work (not the math, the mechanics):**
You need to understand: what a context window is and why it matters. What "tokenization" means. Why LLMs are stateless. What "hallucination" is and what causes it. What "temperature" and "top-p" affect. What "system prompts" are and how they set up an AI's behavior. What "fine-tuning" means and when it is appropriate. What "RLHF" (Reinforcement Learning from Human Feedback) is. This knowledge is foundational to understanding everything else.

**Priority 2 — How agents and tool use work:**
You need to understand: what happens mechanically when an AI "calls a tool." What a "function call" or "tool call" looks like at the API level. What the difference is between a tool-calling model and a model that cannot use tools. How a framework like LangChain constructs an "agent loop" — the sequence of: observe, reason, decide on action, take action, observe result, reason again.

**Priority 3 — Distributed systems fundamentals (the concepts, not the implementation):**
You need to understand: what it means for a system to be "distributed" (running across multiple machines). What "consistency" means and why it is hard in a distributed system. What "eventual consistency" vs. "strong consistency" means. What "fault tolerance" means. What a "message queue" is and why it makes systems more reliable. What "idempotency" means and why it matters. You do not need to implement any of these — you need to recognize them in proposed architectures and understand why they are there.

**Priority 4 — Security fundamentals:**
You need to understand: what authentication vs. authorization means. What OAuth is and why it is used. What "least privilege" means. What prompt injection is. What an "audit log" is. What a "threat model" is. How to think about an attacker's goals and how architecture decisions mitigate them.

**Priority 5 — Memory and retrieval:**
You need to understand: what a vector embedding is (numbers that represent meaning, such that similar meanings produce similar numbers). What a vector database does (finds items with similar meaning, not exact text matches). What RAG (Retrieval-Augmented Generation) means in practice. What the difference is between semantic search and keyword search.

**Priority 6 — Infrastructure basics:**
You need to understand: what a "container" is and what Docker does. What "serverless" means and what its tradeoffs are. What an "API" is at the HTTP level — not just conceptually. What "latency" means and why it matters. What "horizontal scaling" vs. "vertical scaling" means.

**How to learn these things:**
Each priority should be approached as: find one excellent explanation (article, video, or short course), read/watch it all the way through, then test your understanding by explaining it in your own words without referring to the source. If you cannot explain it simply, you do not yet understand it.

Recommended starting resources: Andrej Karpathy's "Intro to Large Language Models" (YouTube) for Priority 1. The ReAct paper for Priority 2. Martin Fowler's writing on distributed systems patterns for Priority 3. Troy Hunt's work on web security fundamentals for Priority 4. The Pinecone learning center for Priority 5. The "Cloud Computing Concepts" by Indranil Gupta on Coursera for Priority 6.

---

## PART VII — RESEARCH ARTIFACTS

Every stage should produce specific documents. These documents serve two purposes: they preserve the knowledge gained in each stage for future reference, and they force the discipline of answering specific questions rather than accumulating vague understanding.

**Stage 0:** WAX Conceptual Invariants Document — The non-negotiable properties of WAX, stated in testable terms, with tensions identified.

**Stage 1:** Agent Landscape Survey — State-of-the-art capabilities, limitations, failure modes, and implications for WAX. Honest about gaps.

**Stage 2:** Runtime Architecture Research Report — What WAX's intelligent environment must contain, with comparison of existing approaches.

**Stage 3:** Memory Architecture Research Report — Memory types, mechanisms, tradeoffs, and a preliminary memory taxonomy for WAX.

**Stage 4:** Capability Architecture Research Report — Taxonomy of capability types, dynamic discovery analysis, WAX capability exposure model (preliminary).

**Stage 5:** Security Architecture Research Report — Threat model, AI-specific security catalog, authorization model analysis, and hard requirements.

**Stage 6:** Execution Environment Research Report — Sandboxing comparison, isolation requirements, compute architecture implications.

**Stage 7:** Education Mission Research Report — Pedagogical principles, AI tutoring effectiveness evidence, WAX educational philosophy, and WAEC/NECO/JAMB specific analysis.

**Stage 8:** Model Independence Research Report — Provider abstraction analysis, model routing evaluation, and model independence requirements.

**Stage 9:** Long-Running Objectives Research Report — Durable execution analysis, saga pattern application, resumability requirements.

**Stage 10:** Observability Research Report — Observability requirements, AI-specific tracing needs, audit logging design.

**Stage 11:** Scaling Architecture Research Report — Infrastructure trajectory from prototype to global scale, early decisions that affect future scaling.

**Cross-cutting — Decision Records:** For every significant architectural decision, produce a Decision Record that captures: what was decided, what alternatives were considered, what evidence informed the decision, what the risks are, and what would need to change for the decision to be revisited.

**Cross-cutting — Open Questions Log:** A continuously maintained document of questions that have been raised but not yet answered. Questions should be explicitly closed (with the answer and evidence) rather than allowed to silently disappear.

---

## PART VIII — ARCHITECTURE DECISION GATES

These are the points where you must stop researching and make an explicit architectural commitment. At each gate, you should have enough evidence to make the decision and understand the risks.

**Gate 1 — Agent Architecture Decision:**
Triggered after Stage 1. Question: Does WAX build on an existing agent framework (LangChain, LangGraph, AutoGen) or does it design its own agent loop from scratch? Evidence required: hands-on evaluation of at least two existing frameworks against WAX's invariants. Alternatives: build on existing framework (faster, more constrained), build own (more control, much more work). Decisions to make: which framework (if any) to use as a foundation. Decisions to leave open: which specific model to use, which tools to build first.

**Gate 2 — Runtime Approach Decision:**
Triggered after Stage 2. Question: Does WAX build on an existing durable execution platform (Temporal.io, Inngest) or implement its own state management? Evidence required: evaluation of at least one existing platform against WAX's runtime requirements. This is a significant decision with long-term implications.

**Gate 3 — Memory Architecture Decision:**
Triggered after Stage 3. Question: What is WAX's primary memory architecture — RAG, structured retrieval, hybrid, or something else? What is the memory storage technology (vector database + relational, or something else)? Evidence required: evaluation of at least two memory approaches through actual experimentation (not just theoretical analysis).

**Gate 4 — Capability Model Decision:**
Triggered after Stages 4 and 5. Question: How does WAX expose capabilities to the AI — static list, dynamic registry, MCP-based, or something else? Evidence required: evaluation of MCP in practice, security analysis of dynamic capability exposure.

**Gate 5 — Execution Environment Decision:**
Triggered after Stage 6. Question: How does WAX run code and browser interactions — which sandboxing approach, which execution platform? Evidence required: cost and isolation analysis of at least two approaches (E2B, Modal, Firecracker, etc.).

**Gate 6 — First-version architecture freeze:**
After Gates 1-5, with the foundational prototype validated, a stable v1 architecture should be articulated. This is not the final architecture — it will evolve — but it is the architecture stable enough to build the first real system on. After this gate, significant implementation can begin. Before this gate, implementation is always exploratory.

---

## PART IX — RED FLAGS
### "Ways We Could Accidentally Rebuild the Old WAXPREP Problem"

**Red Flag 1 — Ontology Creep:**
You add a "Task" object to the database. Then a "Subject" field on the Task. Then a "Lesson" type. Then a "Student" profile. Each addition seems reasonable in isolation. After six months, every AI interaction is forced through the Task-Subject-Lesson-Student ontology, and you are back where you started. The red flag is: any database table whose concept belongs to a specific domain (education, project management, commerce) rather than universal infrastructure. If you catch yourself adding a "Lesson" table to the core runtime, stop.

**Red Flag 2 — Workflow Lock-In:**
You build a flow for a common request type — "how do I solve this math problem" — that is so efficient that you build more flows for other common types. Each flow is a hardcoded pathway. After enough flows, the AI is rarely allowed to reason about objectives — it just gets routed into a flow. The red flag is: any place where the system decides what kind of thing the user is doing before the AI gets to reason about it.

**Red Flag 3 — Tool Hardcoding:**
You add specific tools for specific purposes: a math calculator for math questions, a biology database for biology questions, a grammar checker for writing questions. The tools are organized by subject. The AI is implicitly expected to recognize the subject and select the appropriate tool. You have rebuilt the subject-topic-lesson architecture, just at the tool level. The red flag is: tools that are organized by domain rather than by capability type.

**Red Flag 4 — Interface Capture:**
WhatsApp is the first interface, and it is optimized heavily. The response format, the interaction patterns, the session model — all are shaped around WhatsApp constraints. When you try to add a web interface later, the underlying system does not fit a richer interface because it was designed for messaging constraints. The red flag is: any architectural decision justified by "WhatsApp requires this."

**Red Flag 5 — Model Dependency:**
You use Claude with tool use, and you find that its tool-use format works very well, so you build your entire capability layer around Claude's specific function-calling format. When you want to try a different model, the entire capability layer must be rewritten. The red flag is: any interface between the AI and the runtime that is specific to one provider's API format.

**Red Flag 6 — The Sycophantic AI:**
You optimize the AI's behavior for user satisfaction metrics — users report being happier when the AI gives them direct answers rather than engaging them in Socratic inquiry. Over time, the AI becomes very good at giving people what they want in the moment, which is not the same as helping them learn. The education mission has been abandoned without anyone explicitly deciding to abandon it. The red flag is: optimizing for user satisfaction as a proxy for educational value.

**Red Flag 7 — Autonomy Without Accountability:**
The AI is given significant autonomy, but the audit trail is inadequate. When something goes wrong — the AI sends an email the user did not intend, makes an API call that costs money, deletes a file — there is no clear record of what happened and why. The red flag is: any capability that has real-world side effects without a complete, immutable audit trail.

**Red Flag 8 — Security as Feature:**
Security is treated as a feature to be added when the system is ready for production, rather than as a foundational architectural constraint. When the security team (or the single developer thinking about security) arrives, they find that the architecture makes proper security expensive or impossible to retrofit. The red flag is: any discussion of capabilities without simultaneous discussion of authorization.

**Red Flag 9 — The Successful Prototype Trap:**
The foundational prototype works impressively. It is shared widely and generates excitement. The team begins building on the prototype's architecture directly, treating its implementation choices as validated architectural decisions. The prototype validated the concept but not the architecture. The red flag is: treating prototype implementation decisions as architectural commitments.

**Red Flag 10 — Premature Education Abandonment:**
The technical ambition of the open-world system becomes so interesting that the education mission — serving Nigerian students — becomes a vague aspiration rather than a concrete priority. Features are prioritized based on architectural interest rather than educational impact. The red flag is: the first deployed version of WAX is less educationally effective than a simple tutoring chatbot would have been.

---

## PART X — FINAL RESEARCH SEQUENCE

Here is the chronological sequence, derived from the dependency analysis:

**Phase 0 — Conceptual Grounding (Week 1-2):**
Complete Stage 0. Produce the WAX Conceptual Invariants Document. Begin personal learning on LLM mechanics (Priority 1 from Part VI). Begin education mission research in parallel (Stage 7, Track A).

**Phase 1 — Landscape Survey (Weeks 3-5):**
Conduct Stage 1 (Agent landscape) and Stage 7 in parallel. By end of Phase 1, you should understand what AI agents can and cannot do today, and what the evidence says about effective AI-assisted learning. Begin personal learning on agent mechanics (Priority 2 from Part VI).

**Phase 2 — Environment and Memory (Weeks 6-10):**
Conduct Stage 2 (Runtime) and Stage 3 (Memory) in sequence. These are the deepest and most important research stages. Begin security landscape research in parallel (Stage 5 track B). Begin personal learning on distributed systems concepts (Priority 3) and security fundamentals (Priority 4).

**Phase 3 — Capability and Security Architecture (Weeks 11-14):**
Conduct Stage 4 (Capability) and Stage 5 (Security) with deep integration between them — capability design and security design must be developed together. This phase produces the first comprehensive picture of what WAX's intelligent core looks like.

**Gate 1 decision:** Which agent architecture to base the prototype on.

**Phase 4 — Execution and Observability (Weeks 15-17):**
Conduct Stage 6 (Execution) and Stage 10 (Observability) in parallel. Begin personal learning on infrastructure basics (Priority 6).

**Gate 2 decision:** Runtime approach.
**Gate 3 decision:** Memory architecture.
**Gate 4 decision:** Capability model.
**Gate 5 decision:** Execution environment.

**Phase 5 — Model Independence and Long-Running Objectives (Weeks 18-20):**
Conduct Stage 8 (Model independence) and Stage 9 (Long-running objectives) in parallel.

**Phase 6 — Synthesis and Architecture (Weeks 21-24):**
Synthesize all research into a WAX Architecture Document. This is not implementation planning — it is the architectural doctrine that answers the central research question from the Foundation document. Hold Architecture Gate 6. The prototype can now be designed and built.

**Phase 7 — Foundational Prototype (Weeks 25-30):**
Build and evaluate the Open Objective Loop prototype. This prototype should surface architectural assumptions that theory could not reveal. Expect the prototype to challenge some decisions from Gate 6. Some gates may need to be reopened based on prototype findings.

**Phase 8 — First Version Architecture (Weeks 31-36):**
Incorporate prototype learnings into a revised, stabilized architecture. This is the architecture that serious engineering begins with. Conduct Stage 11 (Scaling) research at this point — you now know enough about what you are building to research scaling intelligently.

**Phase 9 — Engineering:**
Real implementation begins. At this point, you have: a clear conceptual philosophy, a research-validated architecture, a working prototype that validates the core idea, a security model, a memory design, a capability system design, an observability plan, and a model-independent foundation. Engineering on this foundation is solving known problems, not discovering unknown problems mid-implementation.

---

**One final note, and perhaps the most important one:**

The WAX Foundation document contains an observation that is worth keeping permanently visible: "Do not let an implementation agent silently make foundational philosophical decisions." Every time you engage a researcher, a developer, or an AI assistant to work on WAX, their natural tendency will be to make decisions in order to move forward. Many of those decisions will be defensible. Some will be wrong. A few will silently encode assumptions that contradict the WAX philosophy. The roadmap above is designed to make those decisions visible — to surface them as explicit gates rather than letting them accumulate invisibly inside the codebase.

The research you are about to do is not overhead before the real work. It is the real work. The years saved by avoiding wrong architectural decisions — decisions that must be undone at enormous cost — are worth far more than the weeks spent getting the architecture right in the first place.