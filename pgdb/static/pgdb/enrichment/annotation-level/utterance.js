angular.module('utterancesEnrichment', [
    'pgdb.corpora',
    'pgdb.enrichment'
]).controller('UtterancesEnrichmentCtrl', function ($scope, $rootScope, Enrichment, Corpora, $state, $stateParams) {
	$scope.enrichment = {enrichment_type: "utterances"};

	$scope.save = function(){
		$scope.enrichment.pause_length = $scope.enrichment.pause_length/1000;
		Enrichment.create($stateParams.corpus_id, $scope.enrichment).then(function (res){
			$state.go('enrichment', {corpus_id: $stateParams.corpus_id});
		}).catch(function(res){
			$scope.error_message = res.data;
		});
	};
});