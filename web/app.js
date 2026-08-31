
(function () {
  var state = {
    view: 'dashboard',
    activeProjectId: null,
    filter: 'all',
    deleteTarget: null
  };

  var mainContent = document.getElementById('main-content');
  var modalOverlay = document.getElementById('modal-overlay');
  var modalTitle = document.getElementById('modal-title');
  var modalBody = document.getElementById('modal-body');
  var newDropdown = document.getElementById('new-dropdown');

  function renderApp() {
    if (state.view === 'project') {
      var project = Store.getProject(state.activeProjectId);
      if (!project) {
        state.view = 'dashboard';
        state.activeProjectId = null;
        renderApp();
        return;
      }
      var tasks = Store.getTasksByProject(project.id);
      mainContent.innerHTML = Render.projectDetail(project, tasks);
    } else {
      var projects = Store.getProjects();
      var standalone = Store.getStandaloneTasks();
      mainContent.innerHTML = Render.dashboard(projects, standalone, state.filter);
    }
    updateFilterBar();
  }

  function updateFilterBar() {
    var tabs = document.querySelectorAll('.filter-tab');
    tabs.forEach(function (tab) {
      tab.classList.toggle('active', tab.dataset.filter === state.filter);
    });
  }

  function openModal(title, bodyHtml) {
    modalTitle.textContent = title;
    modalBody.innerHTML = bodyHtml;
    modalOverlay.hidden = false;
    wireFormRadios();
    var first = modalBody.querySelector('input[type="text"]');
    if (first) setTimeout(function () { first.focus(); }, 50);
  }

  function closeModal() {
    modalOverlay.hidden = true;
    modalBody.innerHTML = '';
    state.deleteTarget = null;
  }

  function wireFormRadios() {
    var cards = modalBody.querySelectorAll('.radio-card');
    cards.forEach(function (card) {
      var input = card.querySelector('input[type="radio"]');
      if (!input) return;
      input.addEventListener('change', function () {
        cards.forEach(function (c) { c.classList.remove('selected'); });
        if (input.checked) card.classList.add('selected');
      });
    });
  }

  function handleFormSubmit(e) {
    e.preventDefault();
    var form = e.target;
    var type = form.dataset.type;
    var id = form.dataset.id;
    var title = form.elements.title.value.trim();
    if (!title) return;

    var priority = form.elements.priority.value;
    var description = form.elements.description ? form.elements.description.value : '';

    if (type === 'project') {
      if (id) {
        Store.updateProject(id, { title: title, description: description, priority: priority });
      } else {
        Store.createProject({ title: title, description: description, priority: priority });
      }
    } else {
      var projectId = form.elements.projectId ? form.elements.projectId.value : '';
      if (id) {
        Store.updateTask(id, { title: title, description: description, priority: priority, projectId: projectId || null });
      } else {
        Store.createTask({ title: title, description: description, priority: priority, projectId: projectId || null });
      }
    }
    closeModal();
    renderApp();
  }

  function getAction(el) {
    while (el && el !== document.body) {
      if (el.dataset && el.dataset.action) return { action: el.dataset.action, id: el.dataset.id, type: el.dataset.type, el: el };
      el = el.parentElement;
    }
    return null;
  }

  // Main content click delegation
  mainContent.addEventListener('click', function (e) {
    var hit = getAction(e.target);
    if (!hit) return;

    switch (hit.action) {
      case 'open-project':
        state.view = 'project';
        state.activeProjectId = hit.id;
        window.location.hash = '#/project/' + hit.id;
        renderApp();
        break;

      case 'go-back':
        state.view = 'dashboard';
        state.activeProjectId = null;
        window.location.hash = '#/';
        renderApp();
        break;

      case 'toggle-task':
        e.preventDefault();
        Store.toggleTask(hit.id);
        renderApp();
        break;

      case 'toggle-priority':
        e.preventDefault();
        if (hit.type === 'project') {
          var proj = Store.getProject(hit.id);
          if (proj) Store.updateProject(hit.id, { priority: proj.priority === 'signal' ? 'noise' : 'signal' });
        } else {
          var task = Store.getTask(hit.id);
          if (task) Store.updateTask(hit.id, { priority: task.priority === 'signal' ? 'noise' : 'signal' });
        }
        renderApp();
        break;

      case 'edit-project':
        e.stopPropagation();
        var p = Store.getProject(hit.id);
        if (p) openModal('Edit Project', Render.projectForm(p));
        break;

      case 'delete-project':
        e.stopPropagation();
        var dp = Store.getProject(hit.id);
        if (dp) {
          var stats = Store.getProjectStats(hit.id);
          state.deleteTarget = { type: 'project', id: hit.id };
          openModal('Delete Project', Render.deleteConfirm('project', dp.title, stats.total > 0));
        }
        break;

      case 'edit-task':
        var t = Store.getTask(hit.id);
        if (t) openModal('Edit Task', Render.taskForm(t, Store.getProjects()));
        break;

      case 'delete-task':
        var dt = Store.getTask(hit.id);
        if (dt) {
          state.deleteTarget = { type: 'task', id: hit.id };
          openModal('Delete Task', Render.deleteConfirm('task', dt.title, false));
        }
        break;

      case 'create-task-in-project':
        openModal('New Task', Render.taskForm(null, Store.getProjects(), hit.id));
        break;
    }
  });

  // Modal click delegation
  modalOverlay.addEventListener('click', function (e) {
    if (e.target === modalOverlay) { closeModal(); return; }

    var hit = getAction(e.target);
    if (!hit) return;

    switch (hit.action) {
      case 'close-modal':
        closeModal();
        break;

      case 'confirm-delete-all':
        if (state.deleteTarget) {
          if (state.deleteTarget.type === 'project') {
            Store.deleteProject(state.deleteTarget.id, false);
            if (state.view === 'project' && state.activeProjectId === state.deleteTarget.id) {
              state.view = 'dashboard';
              state.activeProjectId = null;
              window.location.hash = '#/';
            }
          } else {
            Store.deleteTask(state.deleteTarget.id);
          }
        }
        closeModal();
        renderApp();
        break;

      case 'confirm-delete-keep':
        if (state.deleteTarget && state.deleteTarget.type === 'project') {
          Store.deleteProject(state.deleteTarget.id, true);
          if (state.view === 'project' && state.activeProjectId === state.deleteTarget.id) {
            state.view = 'dashboard';
            state.activeProjectId = null;
            window.location.hash = '#/';
          }
        }
        closeModal();
        renderApp();
        break;
    }
  });

  // Form submission inside modal
  modalBody.addEventListener('submit', function (e) {
    if (e.target.id === 'entity-form') handleFormSubmit(e);
  });

  // Modal close button
  document.querySelector('.modal-close').addEventListener('click', closeModal);

  // Filter bar
  document.getElementById('filter-bar').addEventListener('click', function (e) {
    if (e.target.classList.contains('filter-tab')) {
      state.filter = e.target.dataset.filter;
      renderApp();
    }
  });

  // New button dropdown
  document.getElementById('btn-new').addEventListener('click', function (e) {
    e.stopPropagation();
    newDropdown.hidden = !newDropdown.hidden;
  });

  document.addEventListener('click', function () {
    newDropdown.hidden = true;
  });

  document.getElementById('new-project-btn').addEventListener('click', function () {
    newDropdown.hidden = true;
    openModal('New Project', Render.projectForm(null));
  });

  document.getElementById('new-task-btn').addEventListener('click', function () {
    newDropdown.hidden = true;
    openModal('New Task', Render.taskForm(null, Store.getProjects()));
  });

  // Keyboard
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      if (!modalOverlay.hidden) closeModal();
      newDropdown.hidden = true;
    }
  });

  // Hash routing
  function handleRoute() {
    var hash = window.location.hash || '#/';
    var projectMatch = hash.match(/^#\/project\/(.+)$/);
    if (projectMatch) {
      state.view = 'project';
      state.activeProjectId = projectMatch[1];
    } else if (hash === '#/signal') {
      state.view = 'dashboard';
      state.filter = 'signal';
    } else if (hash === '#/noise') {
      state.view = 'dashboard';
      state.filter = 'noise';
    } else {
      state.view = 'dashboard';
      if (hash === '#/' || hash === '#') state.filter = 'all';
    }
    renderApp();
  }

  window.addEventListener('hashchange', handleRoute);
  handleRoute();
})();
