<?php

namespace OGame\Http\Controllers\Api\V1;

use Exception;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use OGame\Enums\CharacterClass;
use OGame\Exceptions\ApiException;
use OGame\Services\CharacterClassService;
use OGame\Services\PlayerService;

/**
 * Character classes (Collector, General, Discoverer). The first choice is free, a change costs dark matter.
 * Uses CharacterClassService, the same code as the web UI, so the rules (cost, no change while fleets fly) are the game's own.
 */
class CharacterClassController extends ApiController
{
    /** Names accepted by the API. */
    private const CLASSES = ['collector' => CharacterClass::COLLECTOR, 'general' => CharacterClass::GENERAL, 'discoverer' => CharacterClass::DISCOVERER];

    /**
     * The player's class (null = none yet), what each class gives, and what choosing or changing costs now.
     */
    public function show(PlayerService $player, CharacterClassService $classes): JsonResponse
    {
        return $this->ok($this->state($player, $classes));
    }

    /**
     * Choose a class. Body: "class" = collector, general or discoverer. Free the first time.
     *
     * @throws ApiException
     */
    public function select(Request $request, PlayerService $player, CharacterClassService $classes): JsonResponse
    {
        $input = $this->validated($request->all(), [
            'class' => ['required', 'string', 'in:' . implode(',', array_keys(self::CLASSES))],
        ]);
        $this->assertNotInVacationMode($player);

        try {
            $classes->selectClass($player->getUser(), self::CLASSES[$input['class']]);
        } catch (Exception $e) {
            throw new ApiException('class_change_refused', $e->getMessage(), 422);
        }

        return $this->ok($this->state($player, $classes), 200);
    }

    /**
     * @return array<string, mixed>
     */
    private function state(PlayerService $player, CharacterClassService $classes): array
    {
        $user = $player->getUser();
        $current = $classes->getCharacterClass($user);
        $options = [];
        foreach (self::CLASSES as $name => $class) {
            $options[] = ['class' => $name, 'title' => $class->getName(), 'bonuses' => $class->getBonuses()];
        }

        return [
            'class' => $current === null ? null : array_search($current, self::CLASSES, true),
            'change_cost' => $classes->getChangeCost($user),
            'dark_matter' => (int)($user->dark_matter ?? 0),
            'options' => $options,
        ];
    }
}
