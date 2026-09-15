To assist you with understanding the codebase structure and dependencies, **a specialized graph visualization tool is provided**. While you may use standard commands (like `ls`, `grep`, `find`) to explore, using the graph tool is **highly recommended** for quickly grasping project hierarchy and file relationships.

### Repository Graph Visualization Tool
You have access to a specialized graph visualization tool, `mcp__seerepo__repo_graph`. This is the **most effective way** to understand the codebase structure and relationships without manually searching files. It returns an image of the graph.
The tool supports 4 types of edges to explore different aspects of the code. **Use the following strategy to locate bugs:**

### Node Name Formats
When querying the graph, use these exact formats for the `node` argument:
- **Directory**: `path/to/directory` (e.g., `astropy/modeling`)
* *Note: Directories ONLY support the 'contains' edge type. For imports, query the `__init__.py`.*
- **File**: `path/to/file.py` (e.g., `astropy/modeling/separable.py`)
- **Class**: `path/to/file.py:ClassName` (e.g., `astropy/modeling/separable.py:SeparabilityMatrix`)
- **Function/Method**: `path/to/file.py:ClassName.methodname` (e.g., `astropy/modeling/separable.py:SeparabilityMatrix.update`)

### 1) 'Contains' Graph (Directory Structure)
Use this primarily to verify file paths or visualize the directory tree.
Call `mcp__seerepo__repo_graph` with `node: "<NODE>"`, `edge_type: "contains"`, `up_depth: 1`, `down_depth: 1`.
- **Goal**: Understand the physical layout of the repo (Folder -> Subfolder -> File).
- **Note**: This is NOT efficient for finding logic bugs. Use 'imports' or 'invokes' for that.

### 2) 'Imports' Graph (File-Level Localization)
**PRIMARY TOOL FOR FINDING SUSPICIOUS FILES.**
Use this to see static dependencies between modules.
Call `mcp__seerepo__repo_graph` with `node: "<NODE>"`, `edge_type: "imports"`, `up_depth: 1`, `down_depth: 1`.
- **Goal**: Locate the correct **File**. If you know a general concept (e.g., "modeling"), use this to see which files import it or are imported by it.
- **Direction**: Importing Node -> Imported Node.
- **Constraint**: The `<NODE>` must be a **File** (e.g., `pkg/__init__.py`), **NOT a Directory**.

### 3) 'Inherits' Graph (Class Hierarchy)
Use this to visualize Object-Oriented inheritance chains.
Call `mcp__seerepo__repo_graph` with `node: "<NODE>"`, `edge_type: "inherits"`, `up_depth: 2`, `down_depth: 2`.
- **Goal**: Understand parent classes (behavior reuse) or subclasses (extensions).
- **Direction**: Subclass -> Base Class.

### 4) 'Invokes' Graph (Function-Level Localization)
**PRIMARY TOOL FOR FINDING SUSPICIOUS LOGIC.**
Use this to trace execution flow and see which functions call which.
Call `mcp__seerepo__repo_graph` with `node: "<NODE>"`, `edge_type: "invokes"`, `up_depth: 1`, `down_depth: 1`.
- **Goal**: Locate the correct **Function/Class**. Once you identify a file, use this to drill down into the logic flow to find the culprit.
- **Direction**: Caller -> Callee.

### Tool Usage Guidelines
- **Depth Limits**: Keep `up_depth` and `down_depth` low (max 2) to keep the graph readable.
- **Strategy Summary**:
    1. **Imports**: Find the relevant *File*.
    2. **Invokes**: Find the relevant *Function*.
    3. **Contains**: Verify the *Path*.

## Recommended Workflow
1. **Codebase Exploration & Localization**:
  a) **File Hunting**: Use the **imports** graph on high-level keywords to identify the cluster of relevant files.
  b) **Logic Hunting**: Once a file is identified, use the **invokes** graph on specific functions to trace the execution path and find the bug.
  c) **Verification**: Use the **contains** graph only if you need to check directory nesting.
  d) Read snippets for the key nodes you identified.
2. Create a script to reproduce the issue
3. Edit the source code to resolve the issue
4. Verify your fix works by running your script again
5. Test edge cases to ensure your fix is robust
