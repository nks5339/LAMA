# Stage-Wise Model Routing Guide

## Overview

LAMA now supports **stage-wise model selection** for all providers (Ollama, OpenRouter, Anthropic, OpenAI, Groq), similar to the Factory AI Orchestrator. This allows you to choose different models for each stage of the migration pipeline.

## Configuration Hierarchy

The model resolution follows this priority order:
1. **Agent model override** (per-agent pin in Console → Agents)
2. **Stage routing** (NEW - configure in Console → Models)
3. **Complexity routing** (low/medium/high tier routing)
4. **First available model** (fallback)

## Your Current Configuration

```
Provider: Ollama (local)
Type: ollama
Status: Active | Default
Models: 11 available

┌─────────────────────────────────────────────────────┐
│ STAGE-BASED ROUTING (takes precedence)             │
├─────────────────────────────────────────────────────┤
│ Discovery       → qwen3:4b                (2.5 GB) │
│ DataModel       → llama3.1:8b             (4.9 GB) │
│ Architecture    → qwen2.5-coder:32b      (19 GB)  │
│ CodeGen         → qwen3-coder:30b        (18 GB)  │
│ Living          → llama3.1:8b             (4.9 GB) │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│ COMPLEXITY-BASED ROUTING (fallback)                │
├─────────────────────────────────────────────────────┤
│ LOW      → qwen3:4b                      (2.5 GB)  │
│ MEDIUM   → llama3.1:8b                   (4.9 GB)  │
│ HIGH     → qwen2.5-coder:32b            (19 GB)   │
└─────────────────────────────────────────────────────┘
```

## How It Works

### Stage-Based Routing (NEW)

Each LAMA stage can have its own model:

- **Discovery** → Fast model for chat + SRS generation (Qwen3 4B - lightweight)
- **DataModel** → Balanced model for DDL generation (Llama3.1 8B - reliable)
- **Architecture** → Code-focused for service design (Qwen2.5-Coder 32B - powerful)
- **CodeGen** → Large code model for implementation (Qwen3-Coder 30B - specialized)
- **Living** → Balanced for tests & monitoring (Llama3.1 8B)

### Complexity-Based Routing (Existing)

Within each stage, agents are classified by complexity:
- **Low** = Simple tasks (chat, clarification questions)
- **Medium** = Standard tasks (SRS sections, LLD)
- **High** = Complex tasks (OLTP DDL, HLD, API contracts)

## Configuration Methods

### Method 1: Via API (What We Just Did)

```bash
curl -X PUT http://127.0.0.1:8382/api/console/providers/<PROVIDER_ID> \
  -H "Content-Type: application/json" \
  -d '{
    "stage_routing": {
      "Discovery": "qwen3:4b",
      "DataModel": "llama3.1:8b",
      "Architecture": "qwen2.5-coder:32b",
      "CodeGen": "qwen3-coder:30b",
      "Living": "llama3.1:8b"
    }
  }'
```

### Method 2: Via Console UI (Future)

> **TODO**: Add UI controls in Console → Models page to configure stage_routing.
> Currently requires API calls as shown above.

### Method 3: Leave Empty for Fallback

If you don't set a stage-specific model, the system falls back to complexity-based routing:

```json
{
  "stage_routing": {
    "Discovery": "",  // ← Will use complexity routing instead
    "DataModel": "llama3.1:8b",
    "Architecture": "",  // ← Will use complexity routing
    "CodeGen": "qwen3-coder:30b",
    "Living": ""
  }
}
```

## Benefits

✅ **Cost Optimization** - Use cheaper/smaller models for simple stages  
✅ **Performance Tuning** - Use specialized models (coder variants) for code-heavy stages  
✅ **Resource Management** - Balance VRAM/CPU by distributing load across different models  
✅ **Quality Control** - Use your most powerful model only where it matters (Architecture, CodeGen)

## Example: Optimized for Local Performance

```json
{
  "stage_routing": {
    "Discovery": "qwen3:4b",           // 2.5 GB - Fast chat responses
    "DataModel": "llama3.1:8b",        // 4.9 GB - Good DDL quality
    "Architecture": "qwen2.5-coder:32b", // 19 GB - Best architecture design
    "CodeGen": "qwen3-coder:30b",      // 18 GB - Specialized code generation
    "Living": "llama3.1:8b"            // 4.9 GB - Sufficient for tests
  }
}
```

## Example: Balanced Cloud + Local

```json
{
  "stage_routing": {
    "Discovery": "llama3:latest",                  // Local: Fast
    "DataModel": "claude-sonnet-4.6",              // Cloud: Quality
    "Architecture": "deepseek-v3.1:671b-cloud",    // Cloud: Maximum power
    "CodeGen": "qwen3-coder:30b",                  // Local: Good enough
    "Living": "llama3.1:8b"                        // Local: Fast tests
  }
}
```

## Checking Active Configuration

```bash
# Get all providers
curl -s http://127.0.0.1:8382/api/console/providers | \
  python3 -m json.tool

# Check which model will be used for a specific agent
curl -s http://127.0.0.1:8382/api/console/agents | \
  python3 -m json.tool | \
  grep -A 5 '"key": "srs.generate"'
```

## Troubleshooting

### Model not found (HTTP 404)
- The model name in `stage_routing` doesn't exist in your Ollama
- Fix: Run `ollama list` and use exact model names
- Or: Click "Fetch models" button in Console → Models

### Still using hardcoded model
- Check if agent has a model_override set (takes precedence)
- Clear agent model override: Console → Agents → Edit → Clear model field

### Stage routing not working
- Verify the provider has `stage_routing` field (check via API)
- Restart LAMA after configuration changes
- Check agent's `stage` field matches exactly: "Discovery", "DataModel", etc.

## Migration from Old System

If you were using the old complexity-only routing:

**Before (iter < 13.112)**:
- All routing was tier-based: `{low, medium, high}`
- Hard to optimize per-stage

**After (iter 13.112+)**:
- Stage routing takes precedence
- Complexity routing still works as fallback
- Both can coexist (recommended!)

## See Also

- `backend/models.py` - ModelProvider.stage_routing field
- `backend/fabric/model_fabric.py` - resolve_model() function
- `backend/routes/console.py` - PUT /providers/:id endpoint
- `memory/PRD.md` - Iteration notes (iter 13.112)

---

**Last Updated**: June 25, 2026 (iter 13.112)  
**Feature Status**: ✅ Implemented and functional

