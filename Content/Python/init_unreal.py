import importlib, os, sys
import unreal as ue


def add_bp2mermaid():
    editor_path = os.path.normpath(ue.Paths.project_content_dir() + 'Python/editor')
    if editor_path not in sys.path:
        sys.path.append(editor_path)
    try:
        module = importlib.import_module('bp2mermaid')
        module.register_menu()
    except Exception as error:
        ue.log_error('Failed to register bp2mermaid: ' + str(error))
    return 0


def main():
    add_bp2mermaid()


if __name__ == "__main__":
    main()
