# UI Changes - Stage Routing in Console

## What You'll Now See

Navigate to **Console → Models** page in your browser. For your Ollama provider, you'll now see **TWO routing sections**:

### 1. Complexity Routing (Existing)
```
┌──────────────────────────────────────┐
│ COMPLEXITY ROUTING                   │
├──────────────────────────────────────┤
│ LOW      [qwen3:4b           ▼]      │
│ MEDIUM   [llama3.1:8b        ▼]      │
│ HIGH     [qwen2.5-coder:32b  ▼]      │
└───────────────────────────���──────────┘
```

### 2. Stage Routing (NEW - iter-13.112)
```
┌──────────────────────────────────────┐
│ STAGE ROUTING (overrides complexity)│
├──────────────────────────────────────┤
│ Discovery    [qwen3:4b         ▼]    │
│ DataModel    [llama3.1:8b      ▼]    │
│ Architecture [qwen2.5-coder:32b▼]    │
│ CodeGen      [qwen3-coder:30b  ▼]    │
│ Living       [llama3.1:8b      ▼]    │
└──────────────────────────────────────┘
```

## How to Use

1. **Open LAMA in your browser**: http://127.0.0.1:8382
2. **Navigate to**: Console page (bottom of sidebar)
3. **Click on**: "Models" tab
4. **Scroll to**: Your Ollama provider (should be the first/default one)
5. **Look for**: Two routing sections:
   - "COMPLEXITY ROUTING" (3 rows: low/medium/high)
   - "STAGE ROUTING (overrides complexity)" (5 rows: Discovery/DataModel/Architecture/CodeGen/Living)

## Configuration

### For Each Stage, You Can:

- **Select a specific model** from the dropdown (uses that model always)
- **Leave as "— use complexity routing —"** (falls back to complexity-based selection)

### Current Configuration (Already Set):

Your stage routing is already configured with optimal models:

- **Discovery**: `qwen3:4b` (2.5 GB) - Fast for chat + SRS
- **DataModel**: `llama3.1:8b` (4.9 GB) - Reliable for DDL
- **Architecture**: `qwen2.5-coder:32b` (19 GB) - Best for design
- **CodeGen**: `qwen3-coder:30b` (18 GB) - Specialized for code
- **Living**: `llama3.1:8b` (4.9 GB) - Efficient for tests

## Visual Example

When you open Console → Models, you should see something like:

```
┌─────────────────────────────────────────────────────┐
│ 🖥️  Ollama (local)    ollama    Default            │
│     http://host.docker.internal:11434/v1           │
│     Key: ***                                        │
├─────────────────────────────────────────────────────┤
│ COMPLEXITY ROUTING                                  │
│   LOW      [qwen3:4b                          ▼]   │
│   MEDIUM   [llama3.1:8b                       ▼]   │
│   HIGH     [qwen2.5-coder:32b                 ▼]   │
├─────────────────────────────────────────────────────┤
│ STAGE ROUTING (overrides complexity)               │
│   Discovery    [qwen3:4b                      ▼]   │
│   DataModel    [llama3.1:8b                   ▼]   │
│   Architecture [qwen2.5-coder:32b             ▼]   │
│   CodeGen      [qwen3-coder:30b               ▼]   │
│   Living       [llama3.1:8b                   ▼]   │
├─────────────────────────────────────────────────────┤
│ [Test] [Fetch models] [Set as default] [Edit key] │
└─────────────────────────────────────────────────────┘
```

## Troubleshooting

### If You Don't See Stage Routing Section:

1. **Hard refresh your browser**: 
   - Chrome/Edge: `Ctrl+Shift+R` (Windows) or `Cmd+Shift+R` (Mac)
   - Firefox: `Ctrl+F5` (Windows) or `Cmd+Shift+R` (Mac)

2. **Clear browser cache**:
   - Open DevTools (F12)
   - Right-click the refresh button
   - Select "Empty Cache and Hard Reload"

3. **Verify frontend build timestamp**:
   ```bash
   stat -f '%Sm' /Users/Arindam.Bose1/projects/lama/v1.0/lama-main/frontend/build/index.html
   ```
   Should show: Jun 25 23:24:17 2026 (or later)

4. **Check if changes are served**:
   - Open DevTools (F12) → Console tab
   - Look for any errors
   - Check Network tab to see if files are loading from cache

### If Dropdowns Don't Show Models:

1. **Click "Fetch models"** button to sync with your Ollama
2. **Check Ollama is running**: `ollama list` in terminal
3. **Restart LAMA**: `docker compose restart lama`

## Testing the Configuration

### Verify Stage Routing is Working:

1. **Start a new project** or open existing one
2. **Go to Discovery page**
3. **Open browser DevTools** (F12) → Network tab
4. **Send a chat message**
5. **Look for the LLM request** - it should use `qwen3:4b`
6. **Go to DataModel page** and check the same way - should use `llama3.1:8b`

### Check Agent Model Resolution:

```bash
# See which model each agent will use
curl -s http://127.0.0.1:8382/api/console/agents | \
  python3 -m json.tool | \
  grep -B 3 -A 3 '"resolved_model"'
```

## What Changed

### Backend:
- ✅ `models.py`: Added `stage_routing` field to `ModelProvider`
- ✅ `fabric/model_fabric.py`: Updated `resolve_model()` to check stage routing first
- ✅ `llm.py`: Updated `_resolve_context_window()` for stage-aware token budgets
- ✅ `routes/console.py`: Added `stage_routing` to provider update endpoint

### Frontend:
- ✅ `Console.jsx`: Added new "Stage Routing" section with 5 dropdowns

---

**Build Timestamp**: Jun 25 23:24:17 2026  
**Docker Image**: mishramesh/lama:latest  
**Feature**: Stage-wise Model Routing (iter-13.112)

