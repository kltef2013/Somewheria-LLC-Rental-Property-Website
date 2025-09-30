import random
import pygame
import os

# ==== CONFIG ====
GRID_SIZE = 6
CELL_SIZE = 80
NUM_EPISODES = 30000
MAX_STEPS = 100
EPSILON = 0.2
ALPHA = 0.1
GAMMA = 0.9
NUM_WALLS = 10
FPS = 5

DIRECTIONS = {
    'UP': (0, -1),
    'DOWN': (0, 1),
    'LEFT': (-1, 0),
    'RIGHT': (1, 0),
    'STAY': (0, 0)
}
ACTIONS = list(DIRECTIONS.keys())

def clamp(n, minv, maxv):
    return max(min(n, maxv), minv)

class Entity:
    def __init__(self, x, y, symbol):
        self.x = x
        self.y = y
        self.symbol = symbol

    def move(self, dx, dy, grid):
        new_x, new_y = self.x + dx, self.y + dy
        if 0 <= new_x < GRID_SIZE and 0 <= new_y < GRID_SIZE:
            if grid[new_y][new_x] == ' ':
                grid[self.y][self.x] = ' '
                self.x, self.y = new_x, new_y
                grid[self.y][self.x] = self.symbol
                return True
        return False

def create_grid():
    return [[' ' for _ in range(GRID_SIZE)] for _ in range(GRID_SIZE)]

def place_static_objects(grid, symbol, count):
    placed = 0
    while placed < count:
        x, y = random.randint(0, GRID_SIZE - 1), random.randint(0, GRID_SIZE - 1)
        if grid[y][x] == ' ':
            grid[y][x] = symbol
            placed += 1

def place_entity(grid, symbol):
    while True:
        x, y = random.randint(0, GRID_SIZE - 1), random.randint(0, GRID_SIZE - 1)
        if grid[y][x] == ' ':
            grid[y][x] = symbol
            return Entity(x, y, symbol)

def get_state(agent, other_agent):
    dx = clamp(other_agent.x - agent.x, -4, 4)
    dy = clamp(other_agent.y - agent.y, -4, 4)
    return (dx, dy)

def choose_action(state, q_table, epsilon=EPSILON):
    if random.random() < epsilon:
        return random.choice(ACTIONS)
    return max(ACTIONS, key=lambda a: q_table.get((state, a), 0))

def update_q_table(q_table, state, action, reward, next_state):
    old_q = q_table.get((state, action), 0)
    next_max = max(q_table.get((next_state, a), 0) for a in ACTIONS)
    new_q = old_q + ALPHA * (reward + GAMMA * next_max - old_q)
    q_table[(state, action)] = new_q

def in_line_of_sight(seeker, hider, grid):
    # Check LOS on the same row or column, blocked by walls
    if seeker.x == hider.x:
        step = 1 if hider.y > seeker.y else -1
        for y in range(seeker.y + step, hider.y, step):
            if grid[y][seeker.x] == '#':
                return False
        return True
    elif seeker.y == hider.y:
        step = 1 if hider.x > seeker.x else -1
        for x in range(seeker.x + step, hider.x, step):
            if grid[seeker.y][x] == '#':
                return False
        return True
    return False

def draw_grid(screen, grid):
    colors = {
        ' ': (240, 240, 240),  # empty light gray
        'S': (70, 130, 180),   # seeker steel blue
        'H': (255, 215, 0),    # hider gold
        '#': (30, 30, 30),     # wall dark gray/black
    }
    font = pygame.font.SysFont(None, 36)
    for y, row in enumerate(grid):
        for x, cell in enumerate(row):
            rect = pygame.Rect(x * CELL_SIZE, y * CELL_SIZE, CELL_SIZE, CELL_SIZE)
            pygame.draw.rect(screen, colors.get(cell, (0, 0, 0)), rect)
            pygame.draw.rect(screen, (200, 200, 200), rect, 1)
            if cell in ['S', 'H', '#']:
                text = font.render(cell, True, (255, 255, 255))
                text_rect = text.get_rect(center=rect.center)
                screen.blit(text, text_rect)

def train_agents():
    q_table_seeker = {}
    q_table_hider = {}

    for episode in range(NUM_EPISODES):
        grid = create_grid()
        place_static_objects(grid, '#', NUM_WALLS)
        seeker = place_entity(grid, 'S')
        hider = place_entity(grid, 'H')

        for step in range(MAX_STEPS):
            # Seeker turn
            state_s = get_state(seeker, hider)
            action_s = choose_action(state_s, q_table_seeker)
            dx_s, dy_s = DIRECTIONS[action_s]
            valid_s = seeker.move(dx_s, dy_s, grid)

            # Hider turn
            state_h = get_state(hider, seeker)
            action_h = choose_action(state_h, q_table_hider)
            dx_h, dy_h = DIRECTIONS[action_h]
            valid_h = hider.move(dx_h, dy_h, grid)

            next_state_s = get_state(seeker, hider)
            next_state_h = get_state(hider, seeker)

            seen = in_line_of_sight(seeker, hider, grid)

            if seen:
                update_q_table(q_table_seeker, state_s, action_s, 100, next_state_s)
                update_q_table(q_table_hider, state_h, action_h, -100, next_state_h)
                break
            else:
                reward_s = -1 if valid_s else -5
                reward_h = 1 if valid_h else -5
                update_q_table(q_table_seeker, state_s, action_s, reward_s, next_state_s)
                update_q_table(q_table_hider, state_h, action_h, reward_h, next_state_h)

    return q_table_seeker, q_table_hider

def test_agents_with_gui(q_table_seeker, q_table_hider):
    pygame.init()
    screen = pygame.display.set_mode((GRID_SIZE * CELL_SIZE, GRID_SIZE * CELL_SIZE))
    clock = pygame.time.Clock()
    pygame.display.set_caption("Hide and Seek AI (Seeker + Hider)")

    grid = create_grid()
    place_static_objects(grid, '#', NUM_WALLS)
    seeker = place_entity(grid, 'S')
    hider = place_entity(grid, 'H')

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False

        screen.fill((255, 255, 255))
        draw_grid(screen, grid)
        pygame.display.flip()

        if in_line_of_sight(seeker, hider, grid):
            print("🎯 Seeker found the hider!")
            pygame.time.wait(1000)
            break

        # Seeker move
        state_s = get_state(seeker, hider)
        action_s = choose_action(state_s, q_table_seeker, epsilon=0)  # greedy
        dx_s, dy_s = DIRECTIONS[action_s]
        seeker.move(dx_s, dy_s, grid)

        # Hider move
        state_h = get_state(hider, seeker)
        action_h = choose_action(state_h, q_table_hider, epsilon=0)  # greedy
        dx_h, dy_h = DIRECTIONS[action_h]
        hider.move(dx_h, dy_h, grid)

        clock.tick(FPS)

    pygame.quit()

if __name__ == "__main__":
    print("Training agents... please wait (this may take a moment)...")
    q_s, q_h = train_agents()
    print("Training complete. Launching GUI test.")
    test_agents_with_gui(q_s, q_h)
