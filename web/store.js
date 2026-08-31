const Store = (function () {
  const PROJECTS_KEY = 'flow_projects';
  const TASKS_KEY = 'flow_tasks';

  function _generateId(prefix) {
    return prefix + '_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7);
  }

  function _load(key) {
    try {
      return JSON.parse(localStorage.getItem(key)) || [];
    } catch {
      return [];
    }
  }

  function _save(key, data) {
    try {
      localStorage.setItem(key, JSON.stringify(data));
    } catch {
      // storage full or unavailable
    }
  }

  function _loadProjects() { return _load(PROJECTS_KEY); }
  function _saveProjects(arr) { _save(PROJECTS_KEY, arr); }
  function _loadTasks() { return _load(TASKS_KEY); }
  function _saveTasks(arr) { _save(TASKS_KEY, arr); }

  return {
    getProjects() {
      return _loadProjects();
    },

    getProject(id) {
      return _loadProjects().find(p => p.id === id) || null;
    },

    createProject({ title, description, priority }) {
      const projects = _loadProjects();
      const now = new Date().toISOString();
      const project = {
        id: _generateId('proj'),
        title: title.trim(),
        description: (description || '').trim(),
        priority: priority || 'noise',
        createdAt: now,
        updatedAt: now
      };
      projects.push(project);
      _saveProjects(projects);
      return project;
    },

    updateProject(id, changes) {
      const projects = _loadProjects();
      const idx = projects.findIndex(p => p.id === id);
      if (idx === -1) return null;
      if (changes.title !== undefined) projects[idx].title = changes.title.trim();
      if (changes.description !== undefined) projects[idx].description = changes.description.trim();
      if (changes.priority !== undefined) projects[idx].priority = changes.priority;
      projects[idx].updatedAt = new Date().toISOString();
      _saveProjects(projects);
      return projects[idx];
    },

    deleteProject(id, keepTasks) {
      const projects = _loadProjects().filter(p => p.id !== id);
      _saveProjects(projects);
      if (keepTasks) {
        this.convertTasksToStandalone(id);
      } else {
        this.deleteTasksByProject(id);
      }
    },

    getTasks() {
      return _loadTasks();
    },

    getTask(id) {
      return _loadTasks().find(t => t.id === id) || null;
    },

    getTasksByProject(projectId) {
      return _loadTasks().filter(t => t.projectId === projectId);
    },

    getStandaloneTasks() {
      return _loadTasks().filter(t => !t.projectId);
    },

    createTask({ title, description, priority, projectId }) {
      const tasks = _loadTasks();
      const now = new Date().toISOString();
      const task = {
        id: _generateId('task'),
        title: title.trim(),
        description: (description || '').trim(),
        priority: priority || 'noise',
        projectId: projectId || null,
        completed: false,
        completedAt: null,
        createdAt: now,
        updatedAt: now
      };
      tasks.push(task);
      _saveTasks(tasks);
      return task;
    },

    updateTask(id, changes) {
      const tasks = _loadTasks();
      const idx = tasks.findIndex(t => t.id === id);
      if (idx === -1) return null;
      if (changes.title !== undefined) tasks[idx].title = changes.title.trim();
      if (changes.description !== undefined) tasks[idx].description = changes.description.trim();
      if (changes.priority !== undefined) tasks[idx].priority = changes.priority;
      if (changes.projectId !== undefined) tasks[idx].projectId = changes.projectId || null;
      tasks[idx].updatedAt = new Date().toISOString();
      _saveTasks(tasks);
      return tasks[idx];
    },

    deleteTask(id) {
      const tasks = _loadTasks().filter(t => t.id !== id);
      _saveTasks(tasks);
    },

    toggleTask(id) {
      const tasks = _loadTasks();
      const idx = tasks.findIndex(t => t.id === id);
      if (idx === -1) return null;
      tasks[idx].completed = !tasks[idx].completed;
      tasks[idx].completedAt = tasks[idx].completed ? new Date().toISOString() : null;
      tasks[idx].updatedAt = new Date().toISOString();
      _saveTasks(tasks);
      return tasks[idx];
    },

    deleteTasksByProject(projectId) {
      const tasks = _loadTasks().filter(t => t.projectId !== projectId);
      _saveTasks(tasks);
    },

    convertTasksToStandalone(projectId) {
      const tasks = _loadTasks();
      tasks.forEach(t => {
        if (t.projectId === projectId) t.projectId = null;
      });
      _saveTasks(tasks);
    },

    getProjectStats(projectId) {
      const tasks = this.getTasksByProject(projectId);
      return {
        total: tasks.length,
        completed: tasks.filter(t => t.completed).length
      };
    }
  };
})();
