const Render = (function () {
  function esc(str) {
    const d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
  }

  function priorityBadge(priority) {
    if (priority === 'signal') {
      return '<span class="priority-badge priority-signal"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg> Signal</span>';
    }
    return '<span class="priority-badge priority-noise"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="1"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="9"/></svg> Noise</span>';
  }

  function progressBar(completed, total) {
    if (total === 0) return '<span class="progress-text">No tasks</span>';
    const pct = Math.round((completed / total) * 100);
    return '<div class="progress-wrap">' +
      '<div class="progress-bar"><div class="progress-fill" style="width:' + pct + '%"></div></div>' +
      '<span class="progress-text">' + completed + ' of ' + total + '</span>' +
      '</div>';
  }

  function sortTasks(tasks) {
    return tasks.slice().sort((a, b) => {
      if (a.completed !== b.completed) return a.completed ? 1 : -1;
      if (a.priority !== b.priority) return a.priority === 'signal' ? -1 : 1;
      return new Date(b.createdAt) - new Date(a.createdAt);
    });
  }

  return {
    dashboard(projects, standaloneTasks, filter) {
      let filteredProjects = projects;
      let filteredTasks = standaloneTasks;

      if (filter === 'signal') {
        filteredProjects = projects.filter(p => p.priority === 'signal');
        filteredTasks = standaloneTasks.filter(t => t.priority === 'signal');
      } else if (filter === 'noise') {
        filteredProjects = projects.filter(p => p.priority === 'noise');
        filteredTasks = standaloneTasks.filter(t => t.priority === 'noise');
      }

      const sortedProjects = filteredProjects.slice().sort((a, b) => {
        if (a.priority !== b.priority) return a.priority === 'signal' ? -1 : 1;
        return new Date(b.createdAt) - new Date(a.createdAt);
      });

      const sortedTasks = sortTasks(filteredTasks);

      let html = '';

      // Projects section
      html += '<section class="section">';
      html += '<div class="section-header"><h2 class="section-title">Projects</h2>';
      html += '<span class="section-count">' + sortedProjects.length + '</span></div>';
      if (sortedProjects.length === 0) {
        html += '<div class="empty-state"><p>No projects yet</p></div>';
      } else {
        html += '<div class="project-grid">';
        sortedProjects.forEach(function (p) {
          const stats = Store.getProjectStats(p.id);
          html += Render.projectCard(p, stats);
        });
        html += '</div>';
      }
      html += '</section>';

      // Tasks section
      html += '<section class="section">';
      html += '<div class="section-header"><h2 class="section-title">Tasks</h2>';
      html += '<span class="section-count">' + sortedTasks.length + '</span></div>';
      if (sortedTasks.length === 0) {
        html += '<div class="empty-state"><p>No standalone tasks</p></div>';
      } else {
        html += '<div class="task-list">';
        sortedTasks.forEach(function (t) {
          html += Render.taskItem(t);
        });
        html += '</div>';
      }
      html += '</section>';

      return html;
    },

    projectDetail(project, tasks) {
      const sorted = sortTasks(tasks);
      const stats = { total: tasks.length, completed: tasks.filter(t => t.completed).length };

      let html = '<div class="detail-header">';
      html += '<button class="btn-back" data-action="go-back"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5M12 19l-7-7 7-7"/></svg></button>';
      html += '<div class="detail-info">';
      html += '<div class="detail-title-row"><h1 class="detail-title">' + esc(project.title) + '</h1>';
      html += priorityBadge(project.priority) + '</div>';
      if (project.description) {
        html += '<p class="detail-desc">' + esc(project.description) + '</p>';
      }
      html += '</div>';
      html += '<div class="detail-actions">';
      html += '<button class="btn-icon" data-action="edit-project" data-id="' + project.id + '" title="Edit"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg></button>';
      html += '<button class="btn-icon btn-danger" data-action="delete-project" data-id="' + project.id + '" title="Delete"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg></button>';
      html += '</div></div>';

      html += '<div class="detail-progress">' + progressBar(stats.completed, stats.total) + '</div>';

      html += '<div class="detail-tasks-header"><h2 class="section-title">Tasks</h2>';
      html += '<button class="btn-small btn-accent" data-action="create-task-in-project" data-id="' + project.id + '">+ Add Task</button></div>';

      if (sorted.length === 0) {
        html += '<div class="empty-state"><p>No tasks in this project</p></div>';
      } else {
        html += '<div class="task-list">';
        sorted.forEach(function (t) {
          html += Render.taskItem(t);
        });
        html += '</div>';
      }

      return html;
    },

    projectCard(project, stats) {
      let html = '<div class="project-card" data-action="open-project" data-id="' + project.id + '">';
      html += '<div class="card-top">';
      html += '<h3 class="card-title">' + esc(project.title) + '</h3>';
      html += '<div class="card-actions" onclick="event.stopPropagation()">';
      html += '<button class="btn-icon-sm" data-action="edit-project" data-id="' + project.id + '" title="Edit"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg></button>';
      html += '<button class="btn-icon-sm btn-danger" data-action="delete-project" data-id="' + project.id + '" title="Delete"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg></button>';
      html += '</div></div>';
      if (project.description) {
        html += '<p class="card-desc">' + esc(project.description) + '</p>';
      }
      html += '<div class="card-bottom">';
      html += priorityBadge(project.priority);
      html += progressBar(stats.completed, stats.total);
      html += '</div></div>';
      return html;
    },

    taskItem(task) {
      const cls = 'task-item' + (task.completed ? ' completed' : '');
      let html = '<div class="' + cls + '">';
      html += '<button class="task-check" data-action="toggle-task" data-id="' + task.id + '">';
      if (task.completed) {
        html += '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>';
      }
      html += '</button>';
      html += '<div class="task-content">';
      html += '<span class="task-title">' + esc(task.title) + '</span>';
      if (task.description) {
        html += '<span class="task-desc">' + esc(task.description) + '</span>';
      }
      html += '</div>';
      html += '<div class="task-meta">';
      html += '<button class="priority-toggle" data-action="toggle-priority" data-id="' + task.id + '" data-type="task">' + priorityBadge(task.priority) + '</button>';
      html += '<button class="btn-icon-sm" data-action="edit-task" data-id="' + task.id + '" title="Edit"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg></button>';
      html += '<button class="btn-icon-sm btn-danger" data-action="delete-task" data-id="' + task.id + '" title="Delete"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg></button>';
      html += '</div></div>';
      return html;
    },

    projectForm(project) {
      const isEdit = !!project;
      const title = isEdit ? esc(project.title) : '';
      const desc = isEdit ? esc(project.description) : '';
      const sig = isEdit && project.priority === 'signal';

      let html = '<form id="entity-form" data-type="project" data-id="' + (isEdit ? project.id : '') + '">';
      html += '<div class="form-group"><label for="f-title">Title</label>';
      html += '<input type="text" id="f-title" name="title" value="' + title + '" required autocomplete="off" placeholder="Project name"></div>';
      html += '<div class="form-group"><label for="f-desc">Description <span class="optional">(optional)</span></label>';
      html += '<textarea id="f-desc" name="description" rows="2" placeholder="Brief description">' + desc + '</textarea></div>';
      html += '<fieldset class="form-group"><legend>Priority</legend>';
      html += '<label class="radio-card' + (sig ? ' selected' : '') + '"><input type="radio" name="priority" value="signal"' + (sig ? ' checked' : '') + '>';
      html += '<span class="radio-label"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg> Signal</span>';
      html += '<span class="radio-desc">High priority &mdash; do this first</span></label>';
      html += '<label class="radio-card' + (!sig ? ' selected' : '') + '"><input type="radio" name="priority" value="noise"' + (!sig ? ' checked' : '') + '>';
      html += '<span class="radio-label"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="1"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="9"/></svg> Noise</span>';
      html += '<span class="radio-desc">Lower priority &mdash; everything else</span></label>';
      html += '</fieldset>';
      html += '<div class="form-actions"><button type="button" class="btn btn-secondary" data-action="close-modal">Cancel</button>';
      html += '<button type="submit" class="btn btn-primary">' + (isEdit ? 'Save Changes' : 'Create Project') + '</button></div>';
      html += '</form>';
      return html;
    },

    taskForm(task, projects, presetProjectId) {
      const isEdit = !!task;
      const title = isEdit ? esc(task.title) : '';
      const desc = isEdit ? esc(task.description) : '';
      const sig = isEdit ? task.priority === 'signal' : false;
      const pid = isEdit ? (task.projectId || '') : (presetProjectId || '');

      let html = '<form id="entity-form" data-type="task" data-id="' + (isEdit ? task.id : '') + '">';
      html += '<div class="form-group"><label for="f-title">Title</label>';
      html += '<input type="text" id="f-title" name="title" value="' + title + '" required autocomplete="off" placeholder="Task name"></div>';
      html += '<div class="form-group"><label for="f-desc">Description <span class="optional">(optional)</span></label>';
      html += '<textarea id="f-desc" name="description" rows="2" placeholder="Details">' + desc + '</textarea></div>';
      html += '<div class="form-group"><label for="f-project">Project <span class="optional">(optional)</span></label>';
      html += '<select id="f-project" name="projectId"><option value="">Standalone task</option>';
      (projects || []).forEach(function (p) {
        html += '<option value="' + p.id + '"' + (p.id === pid ? ' selected' : '') + '>' + esc(p.title) + '</option>';
      });
      html += '</select></div>';
      html += '<fieldset class="form-group"><legend>Priority</legend>';
      html += '<label class="radio-card' + (sig ? ' selected' : '') + '"><input type="radio" name="priority" value="signal"' + (sig ? ' checked' : '') + '>';
      html += '<span class="radio-label"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg> Signal</span>';
      html += '<span class="radio-desc">High priority &mdash; do this first</span></label>';
      html += '<label class="radio-card' + (!sig ? ' selected' : '') + '"><input type="radio" name="priority" value="noise"' + (!sig ? ' checked' : '') + '>';
      html += '<span class="radio-label"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="1"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="9"/></svg> Noise</span>';
      html += '<span class="radio-desc">Lower priority &mdash; everything else</span></label>';
      html += '</fieldset>';
      html += '<div class="form-actions"><button type="button" class="btn btn-secondary" data-action="close-modal">Cancel</button>';
      html += '<button type="submit" class="btn btn-primary">' + (isEdit ? 'Save Changes' : 'Create Task') + '</button></div>';
      html += '</form>';
      return html;
    },

    deleteConfirm(type, title, hasChildren) {
      let html = '<div class="delete-confirm">';
      html += '<p>Are you sure you want to delete <strong>' + esc(title) + '</strong>?</p>';
      if (type === 'project' && hasChildren) {
        html += '<p class="delete-note">This project has tasks. What should happen to them?</p>';
        html += '<div class="form-actions">';
        html += '<button type="button" class="btn btn-secondary" data-action="close-modal">Cancel</button>';
        html += '<button type="button" class="btn btn-outline" data-action="confirm-delete-keep" >Keep Tasks</button>';
        html += '<button type="button" class="btn btn-danger" data-action="confirm-delete-all">Delete All</button>';
        html += '</div>';
      } else {
        html += '<div class="form-actions">';
        html += '<button type="button" class="btn btn-secondary" data-action="close-modal">Cancel</button>';
        html += '<button type="button" class="btn btn-danger" data-action="confirm-delete-all">Delete</button>';
        html += '</div>';
      }
      html += '</div>';
      return html;
    }
  };
})();
