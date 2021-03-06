angular.module('corpusDetail', [
    'iscan.corpora',
    'iscan.enrichment',
    'iscan.query',
    'iscan.errors'
])
    .controller('CorpusDetailCtrl', function ($scope, Corpora, $state, $stateParams, Query, $timeout, $rootScope, djangoAuth, Users, Errors) {

        var loadTime = 10000, //Load the data every second
            errorCount = 0, //Counter for the server errors
            loadPromise, runcheck = true; //Pointer to the promise created by the Angular $timout service

        $scope.properties = {};
        $scope.subsets = {};
        $scope.queryIds = {};
        $scope.available_queries = {};
        $scope.task_id = '';

        var getData = function () {
            Corpora.one($stateParams.corpus_id).then(function (res) {
            $scope.corpus = res.data;
            $rootScope.$broadcast('corpus_changed', $scope.corpus);
            console.log($scope.corpus);

            if ($scope.corpus.imported) {
                Query.type_queries($stateParams.corpus_id, 'utterance').then(function (res) {
                    $scope.available_queries.utterance = res.data;
                    console.log($scope.available_queries)
                });
                Query.type_queries($stateParams.corpus_id, 'word').then(function (res) {
                    $scope.available_queries.word = res.data;
                    console.log($scope.available_queries)
                });
                Query.type_queries($stateParams.corpus_id, 'syllable').then(function (res) {
                    $scope.available_queries.syllable = res.data;
                    console.log($scope.available_queries)
                });
                Query.type_queries($stateParams.corpus_id, 'phone').then(function (res) {
                    $scope.available_queries.phone = res.data;
                    console.log($scope.available_queries)
                });

                Corpora.hierarchy($stateParams.corpus_id).then(function (res) {
                    $scope.hierarchy = res.data;
                    console.log($scope.hierarchy);
                    for (var a_idx = 0; a_idx <  $scope.hierarchy.annotation_types.length; a_idx++) {
			var atype = $scope.hierarchy.annotation_types[a_idx];
                        $scope.properties[atype] = [];
                        for (j = 0; j < $scope.hierarchy.type_properties[atype].length; j++) {
                            var prop = $scope.hierarchy.type_properties[atype][j][0];
                            if ($scope.properties[atype].indexOf(prop) === -1) {
                                $scope.properties[atype].push(prop)
                            }
                        }
                        for (j = 0; j < $scope.hierarchy.token_properties[atype].length; j++) {
                            var prop = $scope.hierarchy.token_properties[atype][j][0];
                            if ($scope.properties[atype].indexOf(prop) === -1) {
                                $scope.properties[atype].push(prop)
                            }
                        }

                        $scope.subsets[atype] = [];
                        if ($scope.hierarchy.subset_types[atype] !== undefined) {
                            for (j = 0; j < $scope.hierarchy.subset_types[atype].length; j++) {
                                var prop = $scope.hierarchy.subset_types[atype][j];
                                if ($scope.subsets[atype].indexOf(prop) === -1) {
                                    $scope.subsets[atype].push(prop)
                                }
                            }

                        }
                        if ($scope.hierarchy.subset_tokens[atype] !== undefined) {
                            for (j = 0; j < $scope.hierarchy.subset_tokens[atype].length; j++) {
                                var prop = $scope.hierarchy.subset_tokens[atype][j];
                                if ($scope.subsets[atype].indexOf(prop) === -1) {
                                    $scope.subsets[atype].push(prop)
                                }
                            }
                        }
                    }
                    console.log($scope.properties)
                }).catch(function(res){
			console.log($scope.corpus_status)
			$scope.error_message = res.data;
            Corpora.one($stateParams.corpus_id).then(function (res) {
                $scope.corpus = res.data;
            });
		});
            }
            else if ($scope.corpus.busy && $scope.task_id !== ''){

                Errors.checkForErrors($scope.task_id);

            }
            nextLoad(loadTime);
        });
        };

        djangoAuth.authenticationStatus(true).then(function () {

        //Start polling the data from the server
            Users.current_user().then(function (res) {
                $scope.user = res.data;
                $scope.can_query = $scope.user.corpus_permissions[$stateParams.corpus_id].can_query;
                if (!$scope.can_query){
                    $state.go('home');
                }
                $scope.can_enrich = $scope.user.corpus_permissions[$stateParams.corpus_id].can_enrich;
            getData();
            });
        }).catch(function(res){
                $state.go('home');
        });

        var cancelNextLoad = function () {
            $timeout.cancel(loadPromise);
        };

        var nextLoad = function (mill) {
            if (!runcheck){
                return
            }
            console.log('RUNNING NEXT LOAD')
            mill = mill || loadTime;

            //Always make sure the last timeout is cleared before starting a new one
            cancelNextLoad();
            loadPromise = $timeout(getData, mill);
        };



        //Always clear the timeout when the view is destroyed, otherwise it will keep polling and leak memory
        $scope.$on('$destroy', function () {
            console.log('DESTROYING')
            runcheck = false;
            cancelNextLoad();
        });





        $scope.importCorpus = function () {
            Corpora.importCorpus($stateParams.corpus_id).then(function (res) {
                $scope.task_id = res.headers("task");
                getData();
            }).catch(function (res) {
		    console.error(res);
	    });
        };

        $scope.openQuery = function (type) {
            console.log($scope.queryIds[type]);
            $state.go('query', {corpus_id: $stateParams.corpus_id, query_id: $scope.queryIds[type]})
        };
        $scope.newQuery = function (type) {
            $state.go('new_query', {corpus_id: $stateParams.corpus_id, type: type})
        };
        $scope.enrichment = function(){
            $state.go('enrichment', {corpus_id: $stateParams.corpus_id})
        };
    });
