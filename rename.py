import os
import re

replacements = {
    r'\bgenerate_periodization_plan\b': 'plan_generate',
    r'\bapply_periodization_plan\b': 'plan_apply',
    r'\bdelete_plan\b': 'plan_rm',
    r'\bgenerate_workouts\b': 'workout_generate',
    r'\bapply_adaptations\b': 'workout_adapt_apply',
    r'\bvalidate_swap\b': 'workout_swap_validate',
    r'\bapply_swap\b': 'workout_swap_apply',
    r'\bbootstrap_workouts\b': 'data_bootstrap',
    r'\breflect_workouts\b': 'data_reflect',
    r'\b_generate_macrocycle_strategy\b': '_plan_generate_strategy',
    r'\b_generate_workouts_logic\b': '_workout_generate_logic',
    r'\b_adapt_logic\b': '_workout_adapt_logic',
    r'\b_analyze_workouts_logic\b': '_data_analyze_logic',
}

def process_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()
    
    new_content = content
    for pattern, repl in replacements.items():
        new_content = re.sub(pattern, repl, new_content)
    
    # Special case for adapt, as it's a common word
    # Only replace coach_service.adapt and def workout_adapt(
    new_content = re.sub(r'\bcoach_service\.adapt\(', 'coach_service.workout_adapt(', new_content)
    new_content = re.sub(r'\bdef adapt\(', 'def workout_adapt(', new_content)

    if new_content != content:
        with open(filepath, 'w') as f:
            f.write(new_content)
        print(f"Updated {filepath}")

for root, _, files in os.walk('.'):
    if 'venv' in root or '.git' in root or '__pycache__' in root:
        continue
    for file in files:
        if file.endswith('.py'):
            process_file(os.path.join(root, file))

