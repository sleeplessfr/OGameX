<?php

namespace OGame\Http\Controllers\Api\V1;

use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use OGame\Exceptions\ApiException;
use OGame\Models\User;
use OGame\Services\MerchantService;
use OGame\Services\PlayerService;

/**
 * The resource merchant. Calling one costs dark matter and shows random exchange rates; one trade
 * can then be made with it, after which it leaves. These endpoints use MerchantService, the same
 * code as the web UI, so the rules are the game's own.
 *
 * The active merchant is kept in the cache under "active_merchant_<player id>", like the web UI does.
 */
class MerchantController extends ApiController
{
    private const RESOURCES = ['metal', 'crystal', 'deuterium'];

    /**
     * Dark matter, the price of a call, and the merchant that is currently waiting (if any).
     */
    public function show(PlayerService $player): JsonResponse
    {
        return $this->ok($this->state($player));
    }

    /**
     * Call a merchant. Body: "type" = the resource you want to SELL to him (metal, crystal or deuterium).
     * He pays in the other two. Costs dark matter. Refused while another merchant is waiting, so that
     * paid-for rates are not thrown away by accident (trade or dismiss first).
     *
     * @throws ApiException
     */
    public function call(Request $request, PlayerService $player): JsonResponse
    {
        $input = $this->validated($request->all(), [
            'type' => ['required', 'string', 'in:' . implode(',', self::RESOURCES)],
        ]);
        $this->assertNotInVacationMode($player);

        if ($this->activeMerchant($player) !== null) {
            throw new ApiException('merchant_already_active', 'A merchant is already waiting. Trade with him or dismiss him first.', 422);
        }
        if ($this->darkMatter($player) < MerchantService::DARK_MATTER_COST) {
            throw new ApiException('insufficient_dark_matter', 'Not enough dark matter to call a merchant.', 422, [
                'dark_matter' => $this->darkMatter($player),
                'cost' => MerchantService::DARK_MATTER_COST,
            ]);
        }

        $result = MerchantService::callMerchant($player, $input['type']);
        if (!$result['success']) {
            throw new ApiException('merchant_call_failed', $result['message'], 422);
        }

        cache()->forever($this->cacheKey($player), [
            'type' => $input['type'],
            'trade_rates' => $result['tradeRates'] ?? [],
            'called_at' => time(),
        ]);

        return $this->ok($this->state($player), 201);
    }

    /**
     * Trade with the waiting merchant, paid from and delivered to one planet. One trade only: the merchant leaves
     * afterwards. Body: "receive" = {"crystal": 5000, "deuterium": 1000} (how much of each you want; he can only pay
     * in resources other than the one he buys), "give_amount" = the most you are willing to give him. If the
     * wishes cost more than that, or more than the planet has, they are scaled down; if a store is full that
     * resource is skipped.
     *
     * @throws ApiException
     */
    public function trade(Request $request, PlayerService $player, int $planet): JsonResponse
    {
        $planetService = $this->planetFor($player, $planet);
        $input = $this->validated($request->all(), [
            'receive' => ['required', 'array', 'min:1'],
            'receive.*' => ['integer', 'min:1'],
            'give_amount' => ['required', 'integer', 'min:1'],
        ]);
        $this->assertNotInVacationMode($player);

        $merchant = $this->activeMerchant($player);
        if ($merchant === null) {
            throw new ApiException('no_active_merchant', 'No merchant is waiting. Call one first.', 422);
        }
        foreach (array_keys($input['receive']) as $resource) {
            if (!in_array($resource, self::RESOURCES, true) || $resource === $merchant['type']) {
                throw new ApiException('validation_failed', 'The merchant pays only in resources other than the one he buys.', 422, [
                    'receive' => ['Use ' . implode(' and ', array_values(array_diff(self::RESOURCES, [$merchant['type']]))) . '.'],
                ]);
            }
        }

        $result = MerchantService::executeTrade(
            $player,
            $planetService,
            $merchant['type'],
            array_map('intval', $input['receive']),
            (int)$input['give_amount'],
        );
        if (!$result['success']) {
            throw new ApiException('trade_failed', $result['message'], 422);
        }

        cache()->forget($this->cacheKey($player));

        return $this->ok([
            'gave' => [$merchant['type'] => $result['given'] ?? 0],
            'received' => $result['received'] ?? [],
            'dark_matter' => $this->darkMatter($player),
        ]);
    }

    /**
     * Send the waiting merchant away without trading (the dark matter is not refunded).
     */
    public function dismiss(PlayerService $player): JsonResponse
    {
        cache()->forget($this->cacheKey($player));

        return $this->ok($this->state($player));
    }

    /**
     * @return array<string, mixed>
     */
    private function state(PlayerService $player): array
    {
        $merchant = $this->activeMerchant($player);
        $out = null;
        if ($merchant !== null) {
            $rates = [];
            foreach ($merchant['trade_rates']['receive'] ?? [] as $resource => $info) {
                $rate = (float)$info['rate'];
                $rates[$resource] = [
                    'rate' => $rate,
                    // How much of this resource one unit of the sold resource buys (rates are relative to the 3:2:1 base values).
                    'per_unit_given' => round($rate / MerchantService::getBaseRate($merchant['type']), 4),
                    'display' => $info['display'] ?? null,
                ];
            }
            $out = ['type' => $merchant['type'], 'called_at' => $merchant['called_at'] ?? null, 'receive' => $rates];
        }

        return [
            'dark_matter' => $this->darkMatter($player),
            'call_cost' => MerchantService::DARK_MATTER_COST,
            'merchant' => $out,
        ];
    }

    /**
     * @return array{type: string, trade_rates: array<string, mixed>, called_at?: int}|null
     */
    private function activeMerchant(PlayerService $player): array|null
    {
        $merchant = cache()->get($this->cacheKey($player));

        return is_array($merchant) && isset($merchant['type']) ? $merchant : null;
    }

    /**
     * The balance straight from the database: DarkMatterService changes the row, not the user object the
     * PlayerService already holds, so that object can be out of date right after a call.
     */
    private function darkMatter(PlayerService $player): int
    {
        return (int)User::whereKey($player->getId())->value('dark_matter');
    }

    private function cacheKey(PlayerService $player): string
    {
        return 'active_merchant_' . $player->getId();
    }
}
